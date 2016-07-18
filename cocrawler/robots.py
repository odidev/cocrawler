'''
Stuff related to robots.txt processing
'''

import asyncio

from urllib.parse import urlparse
import magic
import time
import json
import robotexclusionrulesparser
import unittest

import stats

class Robots:
    def __init__(self, session, datalayer, config):
        self.session = session
        self.datalayer = datalayer
        self.config = config
        self.rerp = robotexclusionrulesparser.RobotExclusionRulesParser()
        self.max_tries = self.config.get('Robots', {}).get('MaxTries')
        self.in_progress = set()
        self.magic = magic.Magic(flags=magic.MAGIC_MIME_TYPE)
        self.jsonlogfile = self.config.get('Logging', {}).get('Robotslog')
        if self.jsonlogfile:
            self.jsonlogfd = open(self.jsonlogfile, 'w')

    async def check(self, url, actual_robots=None, headers=None):
        parts = urlparse(url)
        try:
            schemenetloc = parts.scheme + '://' + parts.netloc
            if ':' in parts.netloc:
                host, port = parts.netloc.split(':', maxsplit=1)
            else:
                host = parts.netloc
            if parts.path:
                pathplus = parts.path
            else:
                pathplus = '/'
            if parts.params:
                patplus += ';' + parts.params
            if parts.query:
                pathplus += '?' + parts.query
        except:
            schemenetloc = None
            pathplus = None

        if not schemenetloc:
            self.log(url, {'error':'malformed url', 'action':'deny'})
            return False

        try:
            robots = self.datalayer.read_robots_cache(schemenetloc)
        except KeyError:
            robots = await self.fetch_robots(schemenetloc, actual_robots=actual_robots, headers=headers)

        if robots == None:
            self.log(schemenetloc, {'error':'unable to find robots information', 'action':'deny'})
            return False

        if len(robots) == 0:
            return True

        stats.begin_cpu_burn('robots parse')
        self.rerp.parse(robots) # XXX cache this parse?
        stats.end_cpu_burn('robots parse')

#        if self.rerp.sitemaps:
#           ...

        stats.begin_cpu_burn('robots is_allowed')
        check = self.rerp.is_allowed('CoCrawler', pathplus) # XXX proper user-agent
        stats.end_cpu_burn('robots is_allowed')

        if check:
            # don't log success
            return True

        self.log(schemenetloc, {'url':pathplus, 'action':'deny'})
        return False


    async def fetch_robots(self, schemenetloc, actual_robots=None, headers=None):
        '''
        robotexclusionrules parser is not async, so fetch the file ourselves
        '''
        # We might enter this routine multiple times, so, sleep if we aren't the first
        # XXX this is frequently racy, according to the logfiles!
        if schemenetloc in self.in_progress:
            while schemenetloc in self.in_progress:
                print('sleeping because someone beat me to the robots punch', flush=True) # XXX make this a stat
                await asyncio.sleep(1)

            # at this point robots might be in the cache... or not.
            try:
                robots = self.datalayer.read_robots_cache(schemenetloc)
            except KeyError:
                robots = None
            if robots is not None:
                return robots

            # ok, so it's not in the cache -- and the other guy's
            # fetch failed. if we just fell through there would be a
            # big race. treat this as a failure.
            print('some other fetch of robots has failed.') # XXX make this a stat
            return None

        self.in_progress.add(schemenetloc)

        tries = 0
        error = None

        robots = actual_robots
        if not robots:
            robots = schemenetloc + '/robots.txt'

        while tries < self.max_tries:
            try:
                t0 = time.time()
                response = await self.session.get(robots, headers=headers) # allowing redirects
                body_bytes = await response.read()
                apparent_elapsed = '{:.3f}'.format(time.time() - t0)
                break
            except Exception as e:
                error = e
            tries += 1
        else:
            self.log(schemenetloc, {'error':'max tries exceeded, final exception is: ' + str(error),
                                    'action':'fetch'})
            self.in_progress.discard(schemenetloc)
            await response.release()
            return None

        # if we got a 404, return an empty robots.txt
        if response.status == 404:
            self.log(schemenetloc, {'error':'got a 404, treating as empty robots',
                                    'action':'fetch', 'apparent_elapsed':apparent_elapsed})
            self.datalayer.cache_robots(schemenetloc, '')
            self.in_progress.discard(schemenetloc)
            await response.release()
            return ''

        # if we got a non-200, some should be empty and some should be None (XXX Policy)
        if str(response.status).startswith('4') or str(response.status).startswith('5'):
            self.log(schemenetloc, {'error':'got an unexpected status of {}, treating as deny'.format(response.status),
                                    'action':'fetch', 'apparent_elapsed':apparent_elapsed})
            self.in_progress.discard(schemenetloc)
            await response.release()
            return None

        if not self.is_plausible_robots(schemenetloc, body_bytes, apparent_elapsed):
            first10 = body_bytes[:10]
            first10 = urllib.parse.quote(first10)
            self.log(schemenetloc, {'error':'robots file did not look reasonable, treating like empty, initial bytes are: ' + first10,
                                    'action':'fetch', 'apparent_elapsed':apparent_elapsed})
            # this is a warning only; treat the robots as empty.
            self.datalayer.cache_robots(schemenetloc, '')
            self.in_progress.discard(schemenetloc)
            await response.release()
            return ''

        # one last thing... go from bytes to a string, despite bogus utf8
        try:
            body = await response.text()
        except UnicodeError:
            # something went wrong. try again assuming utf8 and ignoring errors
            body = str(body_bytes, 'utf-8', 'ignore')
        except Exception as e:
            # something unusual went wrong. treat like a fetch error.
            self.log(schemenetloc, { 'error':'robots decode threw an exception: ' + str(e),
                                    'action':'fetch', 'apparent_elapsed':apparent_elapsed})
            self.in_progress.discard(schemenetloc)
            await response.release()
            return None

        await response.release()
        self.datalayer.cache_robots(schemenetloc, body)
        self.in_progress.discard(schemenetloc)
        self.log(schemenetloc, {'action':'fetch', 'apparent_elapsed':apparent_elapsed})
        return body

    def is_plausible_robots(self, schemenetloc, body_bytes, apparent_elapsed):
        '''
        Did you know that some sites have a robots.txt that's a 100 megabyte video file?
        '''
        if body_bytes.startswith(b'<'): # html or xml or something else bad
            self.log(schemenetloc, {'error':'robots appears to be html or xml, ignoring',
                                    'action':'fetch', 'apparent_elapsed':apparent_elapsed})
            return False

        # OK: BOM, it signals a text file ... utf8 or utf16 be/le
        # (this info doesn't appear to be recognized by libmagic?!)
        if body_bytes.startswith(b'\xef\xbb\xbf') or body_bytes.startswith(b'\xfe\xff') or body_bytes.startswith(b'\xff\xfe'):
            return True

        # OK: file magic mimetype is 'text'
        mime_type = self.magic.id_buffer(body_bytes)
        if not mime_type.startswith('text'):
            self.log(schemenetloc, {'error':'robots has unexpected mimetype {}, ignoring'.format(mime_type),
                                    'action':'fetch', 'apparent_elapsed':apparent_elapsed})
            return False

        # not OK: too big
        if len(body_bytes) > 1000000:
            self.log(schemenetloc, {'error':'robots is too big, ignoring',
                                    'action':'fetch', 'apparent_elapsed':apparent_elapsed})
            return False

        return True

    def log(self, schemenetloc, d):
        if self.jsonlogfd:
            json_log = d
            json_log['host'] = schemenetloc
            json_log['time'] = '{:.3f}'.format(time.time())
            json_log['who'] = 'robots'
            print(json.dumps(json_log, sort_keys=True), file=self.jsonlogfd, flush=True)

'''
testing of this file is done with end-to-end tests
'''

class TestUrlAlowed(unittest.TestCase):
    def placeholder(self):
        self.assertTrue(True)

if __name__ == '__main__':
    unittest.main()
