import asyncio
import logging
import os
import queue
import signal
import threading

from aiohttp import web
from cachetools import LRUCache
from datetime import datetime, timedelta

from .config import RelayConfig
from .database import RelayDatabase
from .misc import DotDict, check_open_port, request, set_app
from .views import routes


class Application(web.Application):
	def __init__(self, cfgpath):
		web.Application.__init__(self)

		self['starttime'] = None
		self['running'] = False
		self['is_docker'] = bool(os.environ.get('DOCKER_RUNNING'))
		self['config'] = RelayConfig(cfgpath, self['is_docker'])

		if not self['config'].load():
			self['config'].save()

		self['cache'] = DotDict({key: Cache(maxsize=self['config'][key]) for key in self['config'].cachekeys})
		self['semaphore'] = asyncio.Semaphore(self['config'].push_limit)
		self['workers'] = []
		self['last_worker'] = 0

		set_app(self)

		self['database'] = RelayDatabase(self['config'])
		self['database'].load()

		self.set_signal_handler()


	@property
	def cache(self):
		return self['cache']


	@property
	def config(self):
		return self['config']


	@property
	def database(self):
		return self['database']


	@property
	def is_docker(self):
		return self['is_docker']


	@property
	def semaphore(self):
		return self['semaphore']


	@property
	def uptime(self):
		if not self['starttime']:
			return timedelta(seconds=0)

		uptime = datetime.now() - self['starttime']

		return timedelta(seconds=uptime.seconds)


	def push_message(self, inbox, message):
		worker = self['workers'][self['last_worker']]
		worker.queue.put((inbox, message))

		self['last_worker'] += 1

		if self['last_worker'] >= len(self['workers']):
			self['last_worker'] = 0


	def set_signal_handler(self):
		for sig in {'SIGHUP', 'SIGINT', 'SIGQUIT', 'SIGTERM'}:
			try:
				signal.signal(getattr(signal, sig), self.stop)

			# some signals don't exist in windows, so skip them
			except AttributeError:
				pass


	def run(self):
		if not check_open_port(self.config.listen, self.config.port):
			return logging.error(f'A server is already running on port {self.config.port}')

		for route in routes:
			if route[1] == '/stats' and logging.DEBUG < logging.root.level:
				continue

			self.router.add_route(*route)

		logging.info(f'Starting webserver at {self.config.host} ({self.config.listen}:{self.config.port})')
		asyncio.run(self.handle_run())


	def stop(self, *_):
		self['running'] = False


	async def handle_run(self):
		self['running'] = True

		if self.config.workers > 0:
			for i in range(self.config.workers):
				worker = PushWorker(self)
				worker.start()

				self['workers'].append(worker)

		runner = web.AppRunner(self, access_log_format='%{X-Forwarded-For}i "%r" %s %b "%{User-Agent}i"')
		await runner.setup()

		site = web.TCPSite(runner,
			host = self.config.listen,
			port = self.config.port,
			reuse_address = True
		)

		await site.start()
		self['starttime'] = datetime.now()

		while self['running']:
			await asyncio.sleep(0.25)

		await site.stop()

		self['starttime'] = None
		self['running'] = False
		self['workers'].clear()


class Cache(LRUCache):
	def set_maxsize(self, value):
		self.__maxsize = int(value)


class PushWorker(threading.Thread):
	def __init__(self, app):
		threading.Thread.__init__(self)
		self.app = app
		self.queue = queue.Queue()


	def run(self):
		asyncio.run(self.handle_queue())


	async def handle_queue(self):
		while self.app['running']:
			try:
				inbox, message = self.queue.get(block=True, timeout=0.25)
				self.queue.task_done()
				await request(inbox, message)

				logging.verbose(f'New push from Thread-{threading.get_ident()}')

			except queue.Empty:
				pass


## Can't sub-class web.Request, so let's just add some properties
def request_actor(self):
	try: return self['actor']
	except KeyError: pass


def request_instance(self):
	try: return self['instance']
	except KeyError: pass


def request_message(self):
	try: return self['message']
	except KeyError: pass


def request_signature(self):
	if 'signature' not in self._state:
		try: self['signature'] = DotDict.new_from_signature(self.headers['signature'])
		except KeyError: return

	return self['signature']


setattr(web.Request, 'actor', property(request_actor))
setattr(web.Request, 'instance', property(request_instance))
setattr(web.Request, 'message', property(request_message))
setattr(web.Request, 'signature', property(request_signature))

setattr(web.Request, 'cache', property(lambda self: self.app.cache))
setattr(web.Request, 'config', property(lambda self: self.app.config))
setattr(web.Request, 'database', property(lambda self: self.app.database))
setattr(web.Request, 'semaphore', property(lambda self: self.app.semaphore))