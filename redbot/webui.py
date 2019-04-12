#!/usr/bin/env python

"""
A Web UI for RED, the Resource Expert Droid.
"""

from collections import defaultdict
from configparser import SectionProxy
import gzip
import os
import pickle
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Tuple, Union # pylint: disable=unused-import
from urllib.parse import parse_qs, urlsplit
import zlib

import thor
from redbot import __version__
from redbot.message import HttpRequest
from redbot.resource import HttpResource
from redbot.resource.robot_fetch import RobotFetcher
from redbot.formatter import find_formatter, html
from redbot.formatter.html import e_url
from redbot.type import RawHeaderListType, StrHeaderListType # pylint: disable=unused-import


class RedWebUi:
    """
    A Web UI for RED.

    Given a URI, run REDbot on it and present the results to output as HTML.
    If descend is true, spider the links and present a summary.
    """

    _origin_counts = defaultdict(int)   # type: Dict[str, int]
    _origin_period = None               # type: float

    def __init__(self, config: SectionProxy, method: str, query_string: bytes,
                 response_start: Callable[..., None],
                 response_body: Callable[..., None],
                 response_done: Callable[..., None],
                 error_log: Callable[[str], int] = sys.stderr.write) -> None:
        self.config = config  # type: SectionProxy
        self.charset_bytes = self.config['charset'].encode('ascii')
        self.method = method   # Request method to the UX; bytes
        self.response_start = response_start
        self.response_body = response_body
        self._response_done = response_done
        self.error_log = error_log  # function to log errors to
        self.test_uri = None   # type: str
        self.req_hdrs = None   # type: StrHeaderListType
        self.format = None     # type: str
        self.test_id = None    # type: str
        self.check_name = None # type: str
        self.descend = None    # type: bool
        self.save = None       # type: bool
        self.timeout = None    # type: Any
        self.referer_spam_domains = [] # type: List[str]
        if config.get("limit_origin_tests", ""):
            if self._origin_period == None:
                self._origin_period = config.getfloat("limit_origin_period", fallback=1) * 3600
                thor.schedule(self._origin_period, self.ratelimit_cleanup)

        if config.get("referer_spam_domains", ""):
            self.referer_spam_domains = [i.strip() for i in \
                config["referer_spam_domains"].split()]
        self.run(query_string)

    def run(self, query_string: bytes) -> None:
        """Given a bytes query_string from the wire, set attributes."""
        qs = parse_qs(query_string.decode(self.config['charset'], 'replace'))
        self.test_uri = qs.get('uri', [''])[0]
        self.req_hdrs = [tuple(h.split(":", 1)) # type: ignore
                         for h in qs.get("req_hdr", []) if h.find(":") > 0]
        self.format = qs.get('format', ['html'])[0]
        self.descend = 'descend' in qs
        if not self.descend:
            self.check_name = qs.get('check_name', [None])[0]
        self.test_id = qs.get('id', [None])[0]
        if self.method == "POST":
            self.save = 'save' in qs
        else:
            self.save = False
        self.start = time.time()
        if self.save and self.config.get('save_dir', "") and self.test_id:
            self.save_test()
        elif self.test_id:
            self.load_saved_test()
        elif self.test_uri:
            self.run_test()
        else:
            self.show_default()

    def save_test(self) -> None:
        """Save a previously run test_id."""
        try:
            # touch the save file so it isn't deleted.
            os.utime(os.path.join(self.config['save_dir'], self.test_id), (
                thor.time(), thor.time() + (int(self.config['save_days']) * 24 * 60 * 60)))
            location = "?id=%s" % self.test_id
            if self.descend:
                location = "%s&descend=True" % location
            self.response_start("303", "See Other", [("Location", location)])
            self.response_body(
                "Redirecting to the saved test page...".encode(self.config['charset']))
        except (OSError, IOError):
            self.response_start(b"500", b"Internal Server Error",
                                [(b"Content-Type", b"text/html; charset=%s" % self.charset_bytes),])
            self.response_body(self.show_error("Sorry, I couldn't save that."))
        self.response_done([])

    def load_saved_test(self) -> None:
        """Load a saved test by test_id."""
        try:
            fd = gzip.open(os.path.join(self.config['save_dir'], os.path.basename(self.test_id)))
            mtime = os.fstat(fd.fileno()).st_mtime
        except (OSError, IOError, TypeError, zlib.error):
            self.response_start(b"404", b"Not Found", [
                (b"Content-Type", b"text/html; charset=%s" % self.charset_bytes),
                (b"Cache-Control", b"max-age=600, must-revalidate")])
            self.response_body(self.show_error("I'm sorry, I can't find that saved response."))
            self.response_done([])
            return
        is_saved = mtime > thor.time()
        try:
            top_resource = pickle.load(fd)
        except (pickle.PickleError, IOError, EOFError):
            self.response_start(b"500", b"Internal Server Error", [
                (b"Content-Type", b"text/html; charset=%s" % self.charset_bytes),
                (b"Cache-Control", b"max-age=600, must-revalidate")])
            self.response_body(self.show_error("I'm sorry, I had a problem loading that."))
            self.response_done([])
            return
        finally:
            fd.close()

        if self.check_name:
            display_resource = top_resource.subreqs.get(self.check_name, top_resource)
        else:
            display_resource = top_resource

        formatter = find_formatter(self.format, 'html', top_resource.descend)(
            self.config, self.output,
            allow_save=(not is_saved), is_saved=True, test_id=self.test_id)
        content_type = "%s; charset=%s" % (formatter.media_type, self.config['charset'])

        self.response_start(b"200", b"OK", [
            (b"Content-Type", content_type.encode('ascii')),
            (b"Cache-Control", b"max-age=3600, must-revalidate")])
        @thor.events.on(formatter)
        def formatter_done() -> None:
            self.response_done([])
        formatter.bind_resource(display_resource)

    def run_test(self) -> None:
        """Test a URI."""
        if self.config.get('save_dir', "") and os.path.exists(self.config['save_dir']):
            try:
                fd, path = tempfile.mkstemp(prefix='', dir=self.config['save_dir'])
                test_id = os.path.split(path)[1]
            except (OSError, IOError):
                # Don't try to store it.
                test_id = None
        else:
            test_id = None

        top_resource = HttpResource(self.config, descend=self.descend)
        self.timeout = thor.schedule(int(self.config['max_runtime']), self.timeoutError,
                                     top_resource.show_task_map)
        top_resource.set_request(self.test_uri, req_hdrs=self.req_hdrs)
        formatter = find_formatter(self.format, 'html', self.descend)(
            self.config, self.output,
            allow_save=test_id, is_saved=False, test_id=test_id, descend=self.descend)
        content_type = "%s; charset=%s" % (formatter.media_type, self.config['charset'])
        if self.check_name:
            display_resource = top_resource.subreqs.get(self.check_name, top_resource)
        else:
            display_resource = top_resource

        referers = []
        for hdr, value in self.req_hdrs:
            if hdr.lower() == 'referer':
                referers.append(value)
        referer_error = None
        if len(referers) > 1:
            referer_error = "Multiple referers not allowed."
        if referers and urlsplit(referers[0]).hostname in self.referer_spam_domains:
            referer_error = "Referer not allowed."
        if referer_error:
            self.response_start(b"403", b"Forbidden", [
                (b"Content-Type", content_type.encode('ascii')),
                (b"Cache-Control", b"max-age=360, must-revalidate")])
            formatter.start_output()
            formatter.error_output(referer_error)
            self.response_done([])
            return

        if not self.robots_precheck(self.test_uri):
            self.response_start(b"403", b"Forbidden", [
                (b"Content-Type", content_type.encode('ascii')),
                (b"Cache-Control", b"max-age=60, must-revalidate")])
            formatter.start_output()
            formatter.error_output("Forbidden by robots.txt.")
            self.response_done([])
            return

        @thor.events.on(formatter)
        def formatter_done() -> None:
            self.response_done([])
            if test_id:
                try:
                    tmp_file = gzip.open(path, 'w')
                    pickle.dump(top_resource, tmp_file)
                    tmp_file.close()
                except (IOError, zlib.error, pickle.PickleError):
                    pass # we don't cry if we can't store it.
            ti = sum([i.transfer_in for i, t in top_resource.linked], top_resource.transfer_in)
            to = sum([i.transfer_out for i, t in top_resource.linked], top_resource.transfer_out)
            if ti + to > int(self.config['log_traffic']) * 1024:
                self.error_log("%iK in %iK out for <%s> (descend %s)" % (
                    ti / 1024, to / 1024, e_url(self.test_uri), str(self.descend)))

        if self.config.getint('limit_origin_tests', fallback=0):
            testUri = urlsplit(self.test_uri)
            scheme = testUri.scheme.lower()
            authority = testUri.netloc.lower().rsplit("@", 1)[-1]
            if (authority != ""):
                origin = "%s://%s" % (scheme, authority)
                if self._origin_counts.get(origin, 0) > self.config.getint('limit_origin_tests'):
                    self.response_start(b"429", b"Too Many Requests", [
                        (b"Content-Type", content_type.encode('ascii')),
                        (b"Cache-Control", b"max-age=60, must-revalidate")])
                    formatter.start_output()
                    formatter.error_output("Origin is over limit. Please try later.")
                    self.response_done([])
                    self.error_log("origin over limit: %s" % origin)
                    return
                self._origin_counts[origin] += 1

        self.response_start(b"200", b"OK", [
            (b"Content-Type", content_type.encode('ascii')),
            (b"Cache-Control", b"max-age=60, must-revalidate")])
        formatter.bind_resource(display_resource)
        top_resource.check()

    def show_default(self) -> None:
        """Show the default page."""
        formatter = html.BaseHtmlFormatter(self.config, self.output, is_blank=True)
        content_type = "%s; charset=%s" % (formatter.media_type, self.config['charset'])
        self.response_start(b"200", b"OK", [
            (b"Content-Type", content_type.encode('ascii')),
            (b"Cache-Control", b"max-age=300")])
        formatter.start_output()
        formatter.finish_output()
        self.response_done([])

    def show_error(self, message: str, to_output: bool = False) -> Union[None, bytes]:
        """
        Display a message. If to_output is True, send it to self.output(); otherwise
        return it as binary
        """
        out = ("<p class='error'>%s</p>" % message)
        if to_output:
            self.output(out)
            return None
        return out.encode(self.config['charset'], 'replace')

    def output(self, chunk: str) -> None:
        self.response_body(chunk.encode(self.config['charset'], 'replace'))

    def response_done(self, trailers: RawHeaderListType) -> None:
        if self.timeout:
            self.timeout.delete()
            self.timeout = None
        self._response_done(trailers)

    def timeoutError(self, detail: Callable[[], str]) -> None:
        """ Max runtime reached."""
        self.error_log("timeout: <%s> descend=%s; %s" % (
            self.test_uri, self.descend, detail()))
        self.show_error("REDbot timeout.", to_output=True)
        self.response_done([])

    def robots_precheck(self, iri: str) -> bool:
        """
        If we have the robots.txt file available, check it to see if the
        request is permissible.

        This does not fetch robots.txt.
        """
        robot_fetcher = RobotFetcher(self.config)
        try:
            return robot_fetcher.check_robots(HttpRequest.iri_to_uri(iri), sync=True)
        except (UnicodeError, ValueError):
            return True

    def ratelimit_cleanup(self) -> None:
        """
        Clean up ratelimit counters.
        """
        self._origin_counts.clear()
        thor.schedule(self._origin_period, self.ratelimit_cleanup)


# adapted from cgitb.Hook
def except_handler_factory(config: SectionProxy, out: Callable[[str], None] = None,
                           qs: str = None) -> Callable[..., None]:
    """
    Log an exception gracefully.

    config is a config object; out is a function that takes a string; qs is a bytes query string.
    """
    if not out:
        out = sys.stdout.write
    error_template = "<p class='error'>%s</p>"

    def except_handler(etype=None, evalue=None, etb=None): # type: ignore
        """
        Log uncaught exceptions and display a friendly error.
        """
        if not etype or not evalue or not etb:
            etype, evalue, etb = sys.exc_info()
        import cgitb
        out(cgitb.reset())
        if not config.get("exception_dir", ""):
            out(error_template % """
    A problem has occurred, but it probably isn't your fault.
    """)
        else:
            import stat
            import traceback
            if qs:
                doc = "<h3><code>%s</code></h3>" % qs.decode('utf-8', 'replace')
            try:
                doc += cgitb.html((etype, evalue, etb), 5)
            except:                  # just in case something goes wrong
                doc += "<pre>" + ''.join(traceback.format_exception(etype, evalue, etb)) + "</pre>"
            if config.getboolean('debug'):
                out(doc)
                return
            try:
                while etb.tb_next is not None:
                    etb = etb.tb_next
                e_file = etb.tb_frame.f_code.co_filename
                e_line = etb.tb_frame.f_lineno
                ldir = os.path.join(config['exception_dir'], os.path.split(e_file)[-1])
                if not os.path.exists(ldir):
                    os.umask(0o002)
                    os.makedirs(ldir)
                (fd, path) = tempfile.mkstemp(prefix="%s_" % e_line, suffix='.html', dir=ldir)
                fh = os.fdopen(fd, 'w')
                fh.write(doc)
                fh.close()
                os.chmod(path, stat.S_IROTH)
                out(error_template % """\
A problem has occurred, but it probably isn't your fault.
REDbot has remembered it, and we'll try to fix it soon.""")
            except:
                out(error_template % """\
A problem has occurred, but it probably isn't your fault.
REDbot tried to save it, but it couldn't! Oops.<br>
Please e-mail the information below to
<a href='mailto:red@redbot.org'>red@redbot.org</a>
and we'll look into it.""")
                out("<h3>Original Error</h3>")
                out("<pre>")
                out(''.join(traceback.format_exception(etype, evalue, etb)))
                out("</pre>")
                out("<h3>Write Error</h3>")
                out("<pre>")
                out(''.join(traceback.format_exc()))
                out("</pre>")
        sys.exit(1) # We're in an uncertain state, so we must die horribly.

    return except_handler
