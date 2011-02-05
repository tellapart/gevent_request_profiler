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

"""Gevent request profiler.

Enables the discovery of blocking/non-yielding code in request-handling servers
implemented with Gevent. Culprits may include blocking I/O (e.g., file I/O,
native I/O, or non-monkey-patched socket requests) and CPU-intensive code.

Unlike 'cProfile', it is not a deterministic profiler that precisely measures
the run-times of all function calls.  Rather, it identifies 'execution spans',
intervals during which greenlets do not cooperatively yield to other greenlets,
for a set fraction of requests (set at Profiler constructor time).

Because this module defines its own Greenlet and Hub subclasses, it must be
imported and initialized before the first time the hub is used - i.e., before
the first greenlet is started.
"""

import datetime
import random
import time
import traceback
import urllib

import gevent

_OriginalGreenletClass = gevent.Greenlet

class Profiler(object):
  """A Gevent request profiler.
  """
  def __init__(self, request_profiling_pct, record_request_profile_fn=None,
               request_info_class=None):
    """Initialize Gevent profiling.

    Args:
      request_profiling_pct -
        The percent [0.0 - 1.0] of requests to profile.

      record_request_profile_fn -
        An optional function that will be called to record profiling
        information about greenlet behavior that occurred while handling a
        request. The function should accept a ProfilingGreenlet argument and a
        RequestProfile argument. If not specified, profiles will simply be
        printed to stdout.

      request_info_class -
        An optional RequestInfo subclass that will be instantiated to store
        request metadata in the profiler. Defaults to WsgiServerRequestInfo
        (gevent.wsgi); set to PyWsgiServerRequestInfo for gevent.pywsgi or to
        a different subclass for custom request types.
    """
    # Assign the hub type to be the special profiling hub class.
    if gevent.hub._threadlocal.Hub:
      raise ValueError('Hub type already assigned')
    gevent.hub._threadlocal.Hub = ProfilingHub

    # Monkey-patch gevent.Greenlet.
    gevent.Greenlet = gevent.greenlet.Greenlet = ProfilingGreenlet

    ProfilingGreenlet._HUB = gevent.hub.get_hub()

    # Monkey-patch gevent.spawn*().
    for attr in ('spawn', 'spawn_later', 'spawn_link', 'spawn_link_value',
                 'spawn_link_exception'):
      setattr(gevent, attr, getattr(ProfilingGreenlet, attr))

    ProfilingGreenlet._REQUEST_PROFILING_PCT = request_profiling_pct

    ProfilingGreenlet._REQUEST_INFO_CLASS = \
      request_info_class if request_info_class else WsgiServerRequestInfo

    ProfilingGreenlet._RECORD_REQUEST_PROFILE_FN = (
      record_request_profile_fn if record_request_profile_fn
      else _default_record_request_profile)

class ProfilingHub(gevent.hub.Hub):
  """The hub is the greenlet that runs the Gevent event loop.

  This subclass of Hub works together with ProfilingGreenlet to record
  'execution spans' - i.e., intervals during which greenlets do not
  cooperatively yield to other greenlets.
  """
  def __init__(self):
    """Create a ProfilingHub.
    """
    gevent.hub.Hub.__init__(self)

    # A list of (timestamp, ExecutionSpan) tuples in monotonically increasing
    # order.  Timestamps are time.time() (seconds since epoch) values.
    # These will only be recorded for greenlets which are being actively
    # profiled.  This list will be reset to [] whenever no greenlets are
    # being profiled.
    self.exec_spans = []

    # The time.time() (seconds since epoch) value denoting the time when the
    # last greenlet started/stopped/switched.
    self.last_span_time = None

    # The set of request-handling greenlets corresponding to requests currently
    # in progress.
    self.requests_in_progress = set()

    # The set of request-handling greenlets currently performing profiling.
    self.requests_profiling = set()

    # The last ID assigned to a greenlet.
    self.last_assigned_greenlet_id = 0

  def get_next_greenlet_id(self):
    """Returns the next unique greenlet ID.

    Called by the ProfilingGreenlet constructor.
    """
    self.last_assigned_greenlet_id += 1
    return self.last_assigned_greenlet_id

  def is_currently_profiling(self):
    """Returns whether we're currently actively profiling.

    I.e., are there requests currently in progress for which we're recording
    execution spans.
    """
    return len(self.requests_profiling) > 0

  def begin_profiling_request(self, request_greenlet):
    """Turn on profiling for a given server request.

    Args:
      request_greenlet - The ProfilingGreenlet handling the request.
    """
    if not self.requests_profiling:
      self.last_span_time = None

    if request_greenlet.request_info.should_profile:
      self.requests_profiling.add(request_greenlet)

  def finish_profiling_request(self, request_greenlet):
    """Indicate that a request we were profiling has finished.

    Args:
      request_greenlet - The ProfilingGreenlet handling the request.
    """
    if request_greenlet.request_info.should_profile:
      self.requests_profiling.remove(request_greenlet)

    if not self.requests_profiling:
      # We're not in the middle of profiling any more requests; clear the
      # ExecutionSpan list.
      self.exec_spans = []

  def switch(self):
    """Switch from a greenlet back to the hub.

    This method is invoked when a greenlet wishes to cooperatively yield.
    """
    try:
      if self.is_currently_profiling():
        # Before switching back to the hub, record an ExecutionSpan for the
        # greenlet we're switching from.
        self._record_execution_span(gevent.getcurrent())

      return gevent.hub.Hub.switch(self)
    finally:
      if self.is_currently_profiling():
        self.last_span_time = time.time()

  def _record_execution_span(self, glet):
    """Note that a greenlet is yielding.

    Args:
      glet - The ProfilingGreenlet giving up control flow.
    """
    now = time.time()

    if self.last_span_time is None:
      self.last_span_time = now
      return

    if not isinstance(glet, ProfilingGreenlet):
      # The hub greenlet is an instance of greenlet.greenlet rather than
      # ProfilingGreenlet.
      return

    finished = glet.ready()

    span = ExecutionSpan(glet, self.last_span_time, now,
                         traceback.extract_stack(), finished)
    self.exec_spans.append((now, span))

    self.last_span_time = now

class ProfilingGreenlet(_OriginalGreenletClass):
  """Greenlet subclass that works together with ProfilingHub to record
  cooperative yields from one greenlet to another.

  Greenlets instantiated with this class will notify the hub when they finish
  executing (whether normally or with a raised exception).  The hub treats this
  notification as a 'switch' because, even though switch() is not called,
  control flow switches to a different greenlet.
  """
  # The one Hub object. Assigned in Profiler ctor.
  _HUB = None

  # The percent of requests for which profiling should be enabled.
  # Assigned in Profiler ctor.
  _REQUEST_PROFILING_PCT = None

  # The RequestInfo subclass to instantiate. Assigned in Profiler ctor.
  _REQUEST_INFO_CLASS = None

  # The function called to record a RequestProfile. Assigned in Profiler ctor.
  _RECORD_REQUEST_PROFILE_FN = None

  def __init__(self, *args, **kwargs):
    """Create a ProfilingGreenlet.

    Accepts the same arguments as the Greenlet constructor.
    """
    _OriginalGreenletClass.__init__(self, *args, **kwargs)

    if type(ProfilingGreenlet._HUB) != ProfilingHub:
      raise ValueError(
        'Profiler must be instantiated before creating a ProfilingGreenlet')

    # Assign an integer ID to each greenlet, unique while the server process
    # is live.
    self.greenlet_id = ProfilingGreenlet._HUB.get_next_greenlet_id()

    # Obtain the fully-qualified name of the function this greenlet invokes.
    self.fn_name = None
    if args:
      run = args[0]
    else:
      run = kwargs.get('run')
    if run is not None:
      class_name = run.__module__
      if hasattr(run, 'im_class'):
        class_name += '.' + run.im_class.__name__

      self.fn_name = class_name + '.' + run.__name__

    # Only assigned if this is a request-handling greenlet.
    self.first_exec_span_index = 0
    self.request_info = ProfilingGreenlet._REQUEST_INFO_CLASS(self)

  def run(self):
    """Run the callable associated with this greenlet.
    """
    if self.request_info.is_request:
      ProfilingGreenlet._HUB.requests_in_progress.add(self)

    if self.request_info.should_profile:
      ProfilingGreenlet._HUB.begin_profiling_request(self)
      self.first_exec_span_index = len(ProfilingGreenlet._HUB.exec_spans)

    profiling_active = ProfilingGreenlet._HUB.is_currently_profiling()

    if profiling_active:
      greenlet_start_time = ProfilingGreenlet._HUB.last_span_time = time.time()

    try:
      _OriginalGreenletClass.run(self)
    finally:
      if profiling_active:
        # The greenlet finished; record the blocking time elapsed from the last
        # switch through the end of the callable the greenlet was running.
        ProfilingGreenlet._HUB._record_execution_span(self)

      # If this is a request-handling greenlet, output profiling information
      # about greenlet behavior that occurred while handling the request.
      if self.request_info.is_request:
        ProfilingGreenlet._HUB.requests_in_progress.remove(self)

        if profiling_active:
          greenlet_end_time = time.time()

          exec_spans = [
            s[1] for s in ProfilingGreenlet._HUB.exec_spans[
                            self.first_exec_span_index:]]

          profile = RequestProfile(
              self.request_info.path, greenlet_start_time, greenlet_end_time,
              exec_spans)

          ProfilingGreenlet._RECORD_REQUEST_PROFILE_FN(self, profile)
          ProfilingGreenlet._HUB.finish_profiling_request(self)

  def __hash__(self):
    """Return a hash code for this greenlet.

    The hash code is simply the integer greenlet ID.
    """
    return self.greenlet_id

  def __repr__(self):
    """Return a readable string representation of this greenlet.
    """
    return 'ProfilingGreenlet(id=%s)' % self.greenlet_id

class RequestInfo(object):
  """An object storing request metadata used by the profiler.

  An instance of RequestInfo (or a subclass of RequestInfo) is created at
  greenlet instantiation time.
  """
  def __init__(self, profiling_greenlet):
    """Create a RequestInfo.

    Args:
      profiling_greenlet - The greenlet being instantiated.
    """
    self.profiling_greenlet = profiling_greenlet

    self.should_profile = False
    self.is_request = False

    # The request path. Set by a subclass if 'profiling_greenlet' corresponds to
    # a request-handling function.
    self.path = None

class BaseWsgiServerRequestInfo(RequestInfo):
  """Base class for WsgiServerRequestInfo and PyWsgiServerRequestInfo.
  """
  def __init__(self, profiling_greenlet, request_fn_name):
    """Create a BaseWsgiServerRequestInfo.

    Args:
      profiling_greenlet - The greenlet being instantiated.
      request_fn_name - The name of the function expected to be called by
                        request-handling greenlets.
    """
    RequestInfo.__init__(self, profiling_greenlet)

    self.is_request = profiling_greenlet.fn_name == request_fn_name

    if self.is_request:
      self.should_profile = \
        random.random() < ProfilingGreenlet._REQUEST_PROFILING_PCT

class WsgiServerRequestInfo(BaseWsgiServerRequestInfo):
  """Request metadata for gevent.wsgi servers.
  """
  def __init__(self, profiling_greenlet):
    """Create a WsgiServerRequestInfo.

    Args:
      profiling_greenlet - The greenlet being instantiated.
    """
    BaseWsgiServerRequestInfo.__init__(self, profiling_greenlet,
                                       'gevent.wsgi.WSGIServer.handle')

    if self.is_request:
      # Set the request path.
      req = profiling_greenlet.args[0]
      if '?' in req.uri:
          path, query = req.uri.split('?', 1)
      else:
          path, query = req.uri, ''
      self.path = urllib.unquote(path)

class PyWsgiServerRequestInfo(BaseWsgiServerRequestInfo):
  """Request metadata for gevent.pywsgi servers.
  """
  from gevent.pywsgi import WSGIHandler, WSGIServer

  class _WsgiHandler(WSGIHandler):
    """Override the default pywsgi WSGIHandler to set the request path once it's
    available. Unlike gevent.wsgi, gevent.pywsgi doesn't make the request path
    available as an argument to gevent.pywsgi.WsgiServer.handle().
    """
    def handle_one_response(self):
      # Set the request path once it's available.
      gevent.getcurrent().request_info.path = self.environ['PATH_INFO']
      return PyWsgiServerRequestInfo.WSGIHandler.handle_one_response(self)

  WSGIServer.handler_class = _WsgiHandler

  def __init__(self, profiling_greenlet):
    """Create a PyWsgiServerRequestInfo.

    Args:
      profiling_greenlet - The greenlet being instantiated.
    """
    BaseWsgiServerRequestInfo.__init__(self, profiling_greenlet,
                                       'gevent.pywsgi.WSGIServer.handle')

    # The path is set in _WsgiHandler because it's not available as a function
    # argument to gevent.pywsgi.WsgiServer.handle().

class ExecutionSpan(object):
  """Represents a contiguous time interval during which the Gevent event loop
  was blocked while executing a piece of code.

  "Blocking" refers to code that does not cooperatively yield control flow over
  to other greenlets.  Culprits include blocking I/O (e.g., file I/O or
  non-monkey-patched socket requests) and CPU-intensive code.
  """
  def __init__(self, g, start_time, end_time, stack_trace, finished):
    """Create an ExecutionSpan object.

    Args:
      g - The greenlet executing when this span was recorded.
      start_time - The start of the span.  Expressed as float seconds since the
                   start of the epoch.
      end_time - The end of the span.  Expressed as float seconds since the
                 start of the epoch.
      stack_trace - A list of strings (obtained via traceback.extract_stack())
                    representing the traceback from the current stack frame at
                    the moment of yield, i.e., at the moment get_hub().switch()
                    is called.
      finished - A Boolean representing whether this span culminates in a
                 greenlet terminating.
    """
    self.greenlet_id = g.greenlet_id
    self.greenlet_fn_name = g.fn_name
    self.start_time = start_time
    self.end_time = end_time
    self.duration_ms = (end_time - start_time) * 1000.0
    self.stack_trace = stack_trace
    self.finished = finished

  def __repr__(self):
    """Return a readable string representation of this span.
    """
    stack_trace = ''.join(traceback.format_list(self.stack_trace))
    lines = [
      'Span(',
      '  greenlet_id=%s,' % self.greenlet_id,
      '  start_time=%.1f,' % (self.start_time * 1000.0),
      '  end_time=%.1f,' % (self.end_time * 1000.0),
      '  duration=%.1f ms,' % self.duration_ms,
      '  finished=%s,' % self.finished,
      '  fn_name=%s,' % self.greenlet_fn_name,
      '  stack_trace=%s)' % stack_trace,
    ]

    return '\n'.join(lines)

class RequestProfile(object):
  """Encapsulates the profiling information for a sequence of execution spans.
  """
  def __init__(self, request_path, start_time, end_time, exec_spans):
    """Create a RequestProfile.

    Args:
      request_path - The path corresponding to this request.
      start_time - The start time of the Requestprofile, in seconds since the
                   start of the epoch.
      end_time - The end time of the profile, in seconds since the start of
                 the epoch.
      exec_spans - A list of ExecutionSpan objects representing the unyielding
                   spans of execution that occurred while the profile was in
                   progress.
    """
    self.request_path = request_path
    self.start_time = start_time
    self.end_time = end_time
    self.exec_spans = exec_spans

    self.total_duration_ms = 0.0
    self.largest_blocking_span_ms = 0.0

    for span in exec_spans:
      self.total_duration_ms += span.duration_ms
      if span.duration_ms > self.largest_blocking_span_ms:
        self.largest_blocking_span_ms = span.duration_ms

  def _format_epoch_time(self, secs_since_epoch):
    """Formats a 'seconds since the start of the epoch' value into a
    human-readable datetime string.

    Args:
      secs_since_epoch - A float seconds since the start of the epoch value.

    Returns:
      A human readable string.
    """
    dt = datetime.datetime.utcfromtimestamp(secs_since_epoch)
    return '%s.%s UTC' % (dt.strftime('%Y-%m-%d %H:%M:%S'),
                          dt.microsecond)

  def _get_readable_output(self):
    """Return a list of elements to include in a readable string representation
    of this profile.

    Returns:
      A list of strings.
    """
    output = [
      '  Path: %s' % self.request_path,
      '  Start time: %s' % self._format_epoch_time(self.start_time),
      '  End time: %s' % self._format_epoch_time(self.end_time),
      '  Elapsed wall time: %.1f ms' % (
        (self.end_time - self.start_time) * 1000.0),
      '  Elapsed CPU time: %.1f ms'  % self.total_duration_ms,
      '  Number of spans: %s' % len(self.exec_spans),
      '  Longest span CPU time: %.1f ms' % self.largest_blocking_span_ms
    ]

    return output

  def __repr__(self):
    """Return a readable string representation of this RequestProfile.
    """
    output = self._get_readable_output()

    elements = [
      '>>> Begin RequestProfile',
      '\n'.join(output),
      '\n'.join((repr(s) for s in self.exec_spans)),
      '<<< End RequestProfile'
    ]

    return '\n'.join(elements)

def _default_record_request_profile(profiling_greenlet, profile):
  """Default implementation of 'record_request_profile_fn'.

  Simply print the profile to stdout.

  Args:
    profile - A RequestProfile object.
    profiling_greenlet - The greenlet in which to record the RequestProfile.
  """
  print profile
