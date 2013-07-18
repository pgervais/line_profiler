#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import cPickle
from cStringIO import StringIO
import inspect
import linecache
import optparse
import os
import sys
import math

from _line_profiler import LineProfiler as CLineProfiler

CO_GENERATOR = 0x0020


def is_generator(f):
    """ Return True if a function is a generator.
    """
    isgen = (f.func_code.co_flags & CO_GENERATOR) != 0
    return isgen

# Code to exec inside of LineProfiler.__call__ to support PEP-342-style
# generators in Python 2.5+.
pep342_gen_wrapper = '''
def wrap_generator(self, func):
    """ Wrap a generator to profile it.
    """
    def f(*args, **kwds):
        g = func(*args, **kwds)
        # The first iterate will not be a .send()
        self.enable_by_count()
        try:
            item = g.next()
        finally:
            self.disable_by_count()
        input = (yield item)
        # But any following one might be.
        while True:
            self.enable_by_count()
            try:
                item = g.send(input)
            finally:
                self.disable_by_count()
            input = (yield item)
    return f
'''


class LineProfiler(CLineProfiler):
    """ A profiler that records the execution times of individual lines.
    """

    def __call__(self, func):
        """ Decorate a function to start the profiler on function entry and
        stop it on function exit.
        """
        self.add_function(func)
        if is_generator(func):
            f = self.wrap_generator(func)
        else:
            f = self.wrap_function(func)
        f.__module__ = func.__module__
        f.__name__ = func.__name__
        f.__doc__ = func.__doc__
        f.__dict__.update(getattr(func, '__dict__', {}))
        return f

    if sys.version_info[:2] >= (2, 5):
        # Delay compilation because the syntax is not compatible with older
        # Python versions.
        exec pep342_gen_wrapper
    else:
        def wrap_generator(self, func):
            """ Wrap a generator to profile it.
            """
            def f(*args, **kwds):
                g = func(*args, **kwds)
                while True:
                    self.enable_by_count()
                    try:
                        item = g.next()
                    finally:
                        self.disable_by_count()
                    yield item
            return f

    def wrap_function(self, func):
        """ Wrap a function to profile it.
        """
        def f(*args, **kwds):
            self.enable_by_count()
            try:
                result = func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result
        return f

    def dump_stats(self, filename):
        """ Dump a representation of the data to a file as a pickled LineStats
        object from `get_stats()`.
        """
        lstats = self.get_stats()
        f = open(filename, 'wb')
        try:
            cPickle.dump(lstats, f, cPickle.HIGHEST_PROTOCOL)
        finally:
            f.close()

    def print_stats(self, stream=None):
        """ Show the gathered statistics.
        """
        lstats = self.get_stats()
        show_text(lstats.timings, lstats.unit, stream=stream)

    def run(self, cmd):
        """ Profile a single executable statment in the main namespace.
        """
        import __main__
        dict = __main__.__dict__
        return self.runctx(cmd, dict, dict)

    def runctx(self, cmd, globals, locals):
        """ Profile a single executable statement in the given namespaces.
        """
        self.enable_by_count()
        try:
            exec cmd in globals, locals
        finally:
            self.disable_by_count()
        return self

    def runcall(self, func, *args, **kw):
        """ Profile a single function call.
        """
        self.enable_by_count()
        try:
            return func(*args, **kw)
        finally:
            self.disable_by_count()


def format_time(time, unit, human_readable=False):
    units = {0: "s ", 3: "ms", 6: "us", 9: "ns"}
    base_exponent = int(-math.log10(unit + 1e-12) / 3 + 1) * 3
    base_unit = unit / 10 ** (-base_exponent)

    if human_readable:
        time *= base_unit
        current_exponent = base_exponent
        while time >= 1000 and current_exponent > 0:
            current_exponent -= 3
            time /= 1000.
        value = "%.2f %s" % (time, units[current_exponent])
    else:
        value = "%5.1f" % time
    return value


def show_func_html(filename, start_lineno, func_name, timings, unit,
                   stream=None, human_readable=False):
    """ Show results for a single function.
    """
    if stream is None:
        stream = sys.stdout

    # Output header
    print >>stream, ("<h2>%s()</h2>" % func_name)
    print >>stream, "<p>File: %s:%s</p>" % (filename, start_lineno)

    d = {}
    total_time = 0.0
    linenos = []
    for lineno, nhits, time in timings:
        total_time += time
        linenos.append(lineno)
    print >>stream, "<p>Total time: %g s</p>" % (total_time * unit)

    if not os.path.exists(filename):
        raise ValueError("Source file not found: %s" % filename)
    else:
        all_lines = linecache.getlines(filename)
        sublines = inspect.getblock(all_lines[start_lineno - 1:])
    for lineno, nhits, time in timings:
        d[lineno] = (nhits,
                     format_time(time, unit, human_readable=human_readable),
                     format_time(float(time) / nhits, unit,
                                 human_readable=human_readable),
                     '%5.1f' % (100 * time / total_time))
    linenos = range(start_lineno, start_lineno + len(sublines))
    empty = ('', '', '', '')

    # Output timing data
    print >>stream, "<pre>"
    template = '%6s %9s %13s %10s %8s  %-s'
    header = template % ('Line #', 'Hits', 'Time', 'Per Hit', '% Time',
        'Line Contents')
    line_template = '<span class="%s">' + template + '</span>'
    print >>stream, header
    print >>stream, '=' * len(header)
    for lineno, line in zip(linenos, sublines):
        nhits, time, per_hit, percent = d.get(lineno, empty)
        print >>stream, line_template % (percentage_to_class(percent),
                                         lineno, nhits, time, per_hit, percent,
                                         line.rstrip('\n').rstrip('\r'))
    print >>stream, "</pre>"


def percentage_to_class(percentage):
    if len(percentage.strip(" \t")) == 0:
        return "blank"
    p = float(percentage) / 100.
    k = 3.
    category = int(math.ceil((20. * p * (p - k) / (1 - k))))
    if category > 10:
        category = 10
    return "p%d" % category


def show_func(filename, start_lineno, func_name, timings, unit,
              stream=None, human_readable=False):
    """ Show results for a single function.
    """
    if stream is None:
        stream = sys.stdout
    print >>stream, "File: %s" % filename
    print >>stream, "Function: %s at line %s" % (func_name, start_lineno)
    template = '%6s %9s %13s %10s %8s  %-s'
    d = {}
    total_time = 0.0
    linenos = []
    for lineno, nhits, time in timings:
        total_time += time
        linenos.append(lineno)
    print >>stream, "Total time: %g s" % (total_time * unit)
    if not os.path.exists(filename):
        print >>stream, ""
        print >>stream, "Could not find file %s" % filename
        print >>stream, "Are you sure you are running this program from the same directory"
        print >>stream, "that you ran the profiler from?"
        print >>stream, "Continuing without the function's contents."
        # Fake empty lines so we can see the timings, if not the code.
        nlines = max(linenos) - min(min(linenos), start_lineno) + 1
        sublines = [''] * nlines
    else:
        all_lines = linecache.getlines(filename)
        sublines = inspect.getblock(all_lines[start_lineno - 1:])
    for lineno, nhits, time in timings:
        d[lineno] = (nhits,
                     format_time(time, unit, human_readable=human_readable),
                     format_time(float(time) / nhits, unit, human_readable=human_readable),
                     '%5.1f' % (100 * time / total_time))
    linenos = range(start_lineno, start_lineno + len(sublines))
    empty = ('', '', '', '')
    header = template % ('Line #', 'Hits', 'Time', 'Per Hit', '% Time',
        'Line Contents')
    print >>stream, ""
    print >>stream, header
    print >>stream, '=' * len(header)
    for lineno, line in zip(linenos, sublines):
        nhits, time, per_hit, percent = d.get(lineno, empty)
        print >>stream, template % (lineno, nhits, time, per_hit, percent,
            line.rstrip('\n').rstrip('\r'))
    print >>stream, ""


def show_text(stats, unit, stream=None, human_readable=False):
    """ Show text for the given timings.
    """
    if stream is None:
        stream = sys.stdout
    print >>stream, 'Timer unit: %g s' % unit
    print >>stream, ''
    for (fn, lineno, name), timings in sorted(stats.items()):
        show_func(fn, lineno, name, stats[fn, lineno, name],
                  unit, stream=stream, human_readable=human_readable)


def show_html(stats, unit, stream=None, human_readable=False):
    """Output an html file.
    """
    if stream is None:
        stream = sys.stdout

    html_header = """<!doctype html>
    <html>
    <head>
    <style type="text/css">
    span.p0  {background-color: #FFFFFF}
    span.p1  {background-color: #ffffa0}
    span.p2  {background-color: #fff800}
    span.p3  {background-color: #ffde00}
    span.p4  {background-color: #ffbd00}
    span.p5  {background-color: #ff9a00}
    span.p6  {background-color: #ff7800}
    span.p7  {background-color: #ff5700}
    span.p8  {background-color: #ff3700}
    span.p9  {background-color: #ff1d00}
    span.p10 {background-color: #ff0000}
    </style>
    </head>
    <body>
    """
    html_footer = """</body></html>"""
    print >>stream, html_header

    print >>stream, 'Timer unit: %g s' % unit
    print >>stream, ''
    for (fn, lineno, name), timings in sorted(stats.items()):
        show_func_html(fn, lineno, name, stats[fn, lineno, name],
                       unit, stream=stream, human_readable=human_readable)

    print >>stream, html_footer

# A %lprun magic for IPython.
def magic_lprun(self, parameter_s=''):
    """ Execute a statement under the line-by-line profiler from the
    line_profiler module.

    Usage:
      %lprun -f func1 -f func2 <statement>

    The given statement (which doesn't require quote marks) is run via the
    LineProfiler. Profiling is enabled for the functions specified by the -f
    options. The statistics will be shown side-by-side with the code through the
    pager once the statement has completed.

    Options:

    -f <function>: LineProfiler only profiles functions and methods it is told
    to profile.  This option tells the profiler about these functions. Multiple
    -f options may be used. The argument may be any expression that gives
    a Python function or method object. However, one must be careful to avoid
    spaces that may confuse the option parser. Additionally, functions defined
    in the interpreter at the In[] prompt or via %run currently cannot be
    displayed.  Write these functions out to a separate file and import them.

    One or more -f options are required to get any useful results.

    -D <filename>: dump the raw statistics out to a pickle file on disk. The
    usual extension for this is ".lprof". These statistics may be viewed later
    by running line_profiler.py as a script.

    -T <filename>: dump the text-formatted statistics with the code side-by-side
    out to a text file.

    -r: return the LineProfiler object after it has completed profiling.
    """
    # Local import to avoid hard dependency.
    from IPython.genutils import page
    from IPython.ipstruct import Struct
    from IPython.ipapi import UsageError

    # Escape quote markers.
    opts_def = Struct(D=[''], T=[''], f=[])
    parameter_s = parameter_s.replace('"', r'\"').replace("'", r"\'")
    opts, arg_str = self.parse_options(parameter_s, 'rf:D:T:', list_all=True)
    opts.merge(opts_def)

    global_ns = self.shell.user_global_ns
    local_ns = self.shell.user_ns

    # Get the requested functions.
    funcs = []
    for name in opts.f:
        try:
            funcs.append(eval(name, global_ns, local_ns))
        except Exception, e:
            raise UsageError('Could not find function %r.\n%s: %s' % (name,
                e.__class__.__name__, e))

    profile = LineProfiler(*funcs)

    # Add the profiler to the builtins for @profile.
    import __builtin__
    if 'profile' in __builtin__.__dict__:
        had_profile = True
        old_profile = __builtin__.__dict__['profile']
    else:
        had_profile = False
        old_profile = None
    __builtin__.__dict__['profile'] = profile

    try:
        try:
            profile.runctx(arg_str, global_ns, local_ns)
            message = ''
        except SystemExit:
            message = """*** SystemExit exception caught in code being profiled."""
        except KeyboardInterrupt:
            message = ("*** KeyboardInterrupt exception caught in code being "
                "profiled.")
    finally:
        if had_profile:
            __builtin__.__dict__['profile'] = old_profile

    # Trap text output.
    stdout_trap = StringIO()
    profile.print_stats(stdout_trap)
    output = stdout_trap.getvalue()
    output = output.rstrip()

    page(output, screen_lines=self.shell.rc.screen_length)
    print message,

    dump_file = opts.D[0]
    if dump_file:
        profile.dump_stats(dump_file)
        print '\n*** Profile stats pickled to file',\
              repr(dump_file) + '.', message

    text_file = opts.T[0]
    if text_file:
        pfile = open(text_file, 'w')
        pfile.write(output)
        pfile.close()
        print '\n*** Profile printout saved to text file',\
              repr(text_file) + '.', message

    return_value = None
    if opts.has_key('r'):
        return_value = profile

    return return_value


def load_stats(filename):
    """ Utility function to load a pickled LineStats object from a given
    filename.
    """
    f = open(filename, 'rb')
    try:
        lstats = cPickle.load(f)
    finally:
        f.close()
    return lstats


def main():
    usage = "usage: %prog profile.lprof"
    parser = optparse.OptionParser(usage=usage, version='%prog 1.0b2')
    parser.add_option("--human-readable", "-H", dest="human_readable",
                      default=False, action="store_true",
                      help="print execution times in easy to read format")
    parser.add_option("--output-html", "-m", dest="output_html",
                      default=False, action="store_true",
                      help="output results as an html file.")
    options, args = parser.parse_args()
    if len(args) != 1:
        parser.error("Must provide a filename.")
    lstats = load_stats(args[0])

    if options.output_html:
        show_html(lstats.timings, lstats.unit, open("%s.html" % args[0], "wb"),
                  human_readable=options.human_readable)
    else:
        show_text(lstats.timings, lstats.unit,
                  human_readable=options.human_readable)


if __name__ == '__main__':
    main()
