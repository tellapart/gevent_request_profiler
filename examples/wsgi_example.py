#!/usr/bin/env python
# Copyright 2011 TellApart, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This example demonstrates profiling a simple Gevent WSGI server where each
request spawns a few greenlets that burn CPU and yield.
"""

from multiprocessing import Process
import time
import urllib

def main():
  # Run the server in a separate process.
  p = Process(target=_start_server_process)
  p.start()

  # Wait a second for the server to start.
  time.sleep(1.0)

  handle = urllib.urlopen('http://localhost:8088/')
  print 'Response code: ', handle.getcode()

  p.terminate()

def _do_stuff(env, start_response):
  """A WSGI 'application' callable that handles requests. See PEP-333.
  """
  import gevent

  if env['PATH_INFO'] == '/':
    # Spawn a few greenlets and wait for them to finish.
    greenlets = [gevent.spawn(_spin_and_yield) for i in xrange(3)]
    gevent.joinall(greenlets)
    start_response('200 OK', [('Content-Type', 'text/html')])
    return ['did stuff']

  start_response('404 Not Found', [('Content-Type', 'text/html')])
  return ['not found']

def _spin_and_yield():
  """Function that uses up CPU, yields to another greenlet, then uses up
  some more CPU.
  """
  # Burn CPU.
  for i in xrange(10 * 1000):
    a = i * i

  # Yield.
  import gevent
  gevent.sleep(0)

  # Burn some more CPU.
  for i in xrange(10 * 1000):
    a = i * i

def _start_server_process():
  """Launch a Gevent WSGI server, then serve requests forever until receiving
  a SIGTERM (i.e., until terminate() is called on the current process).
  """
  from gevent import monkey
  monkey.patch_all()

  from tellapart.frontend import gevent_profiler
  from tellapart.frontend import util

  # In this example, profile 100% of requests.
  profiler = gevent_profiler.Profiler(request_profiling_pct=1.0)

  util.launch_gevent_wsgi_server(_do_stuff, 8088, 16, 'example server')

if __name__ == '__main__':
  main()
