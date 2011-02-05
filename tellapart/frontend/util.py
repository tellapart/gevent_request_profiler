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

"""Frontend web server utility code.
"""

def launch_gevent_wsgi_server(application, port, max_concurrent_requests,
                              server_name='server', use_pywsgi=False,
                              **kwargs):
  """Set up and launch a Gevent WSGI server in the local process.

  The server will run forever and shut down cleanly when receiving a SIGTERM.

  NOTE: Gevent monkey patching should occur prior to calling this method.

  Args:
    application - A callable that accepts two arguments, per the PEP-333
                  WSGI spec.
    port - Port that the server should run on (integer).
    max_concurrent_requests - The maximum number of concurrent requests
                              to serve (integer).
    server_name - Optional server name to print to logs.
    use_pywsgi - If True, launch a gevent.pywsgi server; if False, launch a
                 gevent.wsgi server.
    **kwargs - Additional keyword args are passed to the WSGIServer ctor.
  """
  import signal
  import gevent
  from gevent import pool

  if use_pywsgi:
    from gevent import pywsgi
    server_class = pywsgi.WSGIServer
  else:
    from gevent import wsgi
    server_class = wsgi.WSGIServer

  wsgi_server = None
  def _shut_down_wsgi_server():
    """Gracefully terminate the WSGI server when receiving a SIGTERM.
    """
    print 'Stopping %s %s' % (server_class.__module__, server_name)

    if wsgi_server:
      wsgi_server.stop()

  gevent.signal(signal.SIGTERM, _shut_down_wsgi_server)

  print 'Starting %s %s' % (server_class.__module__, server_name)

  try:
    greenlet_pool = pool.Pool(max_concurrent_requests)
    wsgi_server = server_class(
      ('', port), application, spawn=greenlet_pool, log=None, **kwargs)
    wsgi_server.serve_forever()
  except KeyboardInterrupt:
    _shut_down_wsgi_server()
