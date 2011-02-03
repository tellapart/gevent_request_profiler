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

import distutils.core

# setuptools is optional but gives us support for, e.g., 'install_requires'.
try:
  import setuptools
except ImportError:
  pass

VERSION = '0.5'
DESCRIPTION = (
  'Enables the discovery of blocking code in request-handling servers '
  'implemented with Gevent.'
)

distutils.core.setup(
  name='tellapart-gevent-profiler',
  version=VERSION,
  description=DESCRIPTION,
  packages=['tellapart', 'tellapart.frontend'],
  author='TellApart',
  author_email='info@tellapart.com',
  url='http://www.tellapart.com/',
  install_requires=['gevent'],
  license='http://www.apache.org/licenses/LICENSE-2.0',
)
