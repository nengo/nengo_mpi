#!/usr/bin/env python
"""
Author:  Eric Crawford (Adapted from mpi4py and nengo)
Contact: eric.crawford@mail.mcgill.ca

This setup script does a bit more than usual setup scripts because it
has to build and install the mpi_sim shared library and its non-python
entry points nengo_mpi and nengo_cpp. An important note is that these files
cannot be built as basic python extension modules for two reasons:

    1. We need to use the mpicxx compiler, and setuptools doesn't allow
       us to use that compiler, even using the --compiler option to the setuptools
        ``build`` command.

    2. The three entry points share most of their object files, but we would
       not be able to easily avoid building each object file repeatedly (once
       for each entry point.

To solve these issues, we just build all three entry points using a Makefile
generated from a template Makefile. ``make`` takes care of all the dependencies
and makes sure the objects files aren't built multiple times. The concrete
Makefile is generated from the template Makefile as part of the configuration
step. This is all achieved this by overriding a few of the usual
distutils/setuptools commands, in particular ``config``, ``build``, ``install``,
``clean``, and adds a new command ``install_mpi``. Care has been taken to override
these commands in such a way that nengo_mpi is fully pip-installable.

"""
from __future__ import print_function
import imp
import sys
import os
import io
import shutil
from glob import glob
from contextlib import contextmanager
import subprocess

try:
    import setuptools
    from setuptools import setup
except ImportError:
    from ez_setup import use_setuptools
    setuptools = use_setuptools()

from setuptools import find_packages, setup  # noqa: F811
from distutils.util import split_quoted
from distutils import log
from distutils.cmd import Command
from distutils.sysconfig import get_python_inc

root = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(root, 'conf'))  # To have access to the contents of dir ``conf``

from mpi_config import get_configuration, configure_compiler, ConfigTest

__doc__ = """ MPI backend for the nengo neural simulator """

MAKEFILE_TEMPLATE = 'Makefile.template'
MAKEFILE_CONFIGURED = 'Makefile.configured'


def import_command(cmd):
    try:
        from importlib import import_module
    except ImportError:
        import_module = lambda n:  __import__(n, fromlist=[None])
    try:
        if not setuptools:
            raise ImportError
        return import_module('setuptools.command.' + cmd)
    except ImportError:
        return import_module('distutils.command.' + cmd)


_config = import_command('config').config
_build = import_command('build').build
_install = import_command('install').install
_develop = import_command('develop').develop
_clean = import_command('clean').clean

pyver = sys.version_info[:2]
if pyver < (2, 6) or (3, 0) <= pyver < (3, 2):
    raise RuntimeError("Python version 2.7 or >= 3.2 required")
if (hasattr(sys, 'pypy_version_info') and sys.pypy_version_info[:2] < (2, 0)):
    raise RuntimeError("PyPy version >= 2.0 required")


def read(*filenames, **kwargs):
    encoding = kwargs.get('encoding', 'utf-8')
    sep = kwargs.get('sep', '\n')
    buf = []
    for filename in filenames:
        with io.open(filename, encoding=encoding) as f:
            buf.append(f.read())
    return sep.join(buf)


version_module = imp.load_source(
    'version', os.path.join(root, 'nengo_mpi', 'version.py'))

description = (
    "An MPI backend for the nengo python package. Supports running "
    "nengo simulations in parallel, using MPI as the communication protocol.")

long_description = read('README.md')

classifiers = """
Intended Audience :: Developers
Intended Audience :: Science/Research
Operating System :: POSIX
Operating System :: POSIX :: Linux
Operating System :: Unix
Programming Language :: C
Programming Language :: Python
Programming Language :: Python :: 2
Programming Language :: Python :: 2.7
Programming Language :: Python :: 3
Programming Language :: Python :: 3.3
Programming Language :: Python :: 3.4
Programming Language :: Python :: 3.5
Programming Language :: Python :: 3.6
Programming Language :: Python :: Implementation :: CPython
Topic :: Scientific/Engineering
Topic :: Software Development :: Libraries :: Python Modules
Topic :: System :: Distributed Computing
"""

keywords = """
neuroscience
computational neuroscience
theoretical neuroscience
scientific computing
parallel computing
message passing interface
MPI
"""

platforms = """
Linux
Unix
"""

metadata = {
    'name'             : 'nengo_mpi',
    'version'          : version_module.version,
    'description'      : description,
    'long_description' : long_description,
    'url'              : "https://github.com/nengo/nengo_mpi",
    'license'          : "See LICENSE.rst",
    'classifiers'      : [c for c in classifiers.split('\n') if c],
    'keywords'         : [k for k in keywords.split('\n')    if k],
    'platforms'        : [p for p in platforms.split('\n')   if p],
    'author'           : 'Eric Crawford',
    'author_email'     : 'eric.crawford@mail.mcgill.ca',
    'maintainer'       : 'Eric Crawford',
    'maintainer_email' : 'eric.crawford@mail.mcgill.ca',
    'install_requires' : ["nengo==2.1",
                          "h5py",
                          "networkx",
                          "pytest>=2.3",
                          "matplotlib>=1.4"]
}

metadata['provides'] = ['nengo_mpi']


class config(_config):
    user_options = [('target=', None, "Target to build. If not supplied, all targets are built.")]

    def initialize_options(self):
        _config.initialize_options(self)
        self.noisy = 0
        self.target = None

    def finalize_options (self):
        _config.finalize_options(self)
        if self.target is None:
            self.target = 'all'
        if not self.noisy:
            self.dump_source = 0

    def _clean(self, *a, **kw):
        _config._clean(self, *a, **kw)

    def check_header(self, header, headers=None, include_dirs=None):
        if headers is None: headers = []
        log.info("checking for header '%s' ..." % header)
        body = "int main(int n, char**v) { (void)n; (void)v; return 0; }"
        ok = self.try_compile(body, list(headers) + [header], include_dirs)
        log.info(ok and 'success!' or 'failure.')
        return ok

    def check_macro(self, macro, headers=None, include_dirs=None):
        log.info("checking for macro '%s' ..." % macro)
        body = ("#ifndef %s\n"
                "#error macro '%s' not defined\n"
                "#endif\n") % (macro, macro)
        body += "int main(int n, char**v) { (void)n; (void)v; return 0; }"
        ok = self.try_compile(body, headers, include_dirs)
        return ok

    def check_library(self, library, library_dirs=None,
                   headers=None, include_dirs=None,
                   other_libraries=[], lang="c"):
        log.info("checking for library '%s' ..." % library)
        body = "int main(int n, char**v) { (void)n; (void)v; return 0; }"
        ok = self.try_link(body,  headers, include_dirs,
                           [library]+other_libraries, library_dirs,
                           lang=lang)
        return ok

    def check_function (self, function,
                        headers=None, include_dirs=None,
                        libraries=None, library_dirs=None,
                        decl=0, call=0, lang="c"):
        log.info("checking for function '%s' ..." % function)
        body = []
        if decl:
            if call: proto = "int %s (void);"
            else:    proto = "int %s;"
            if lang == "c":
                proto = "\n".join([
                        "#ifdef __cplusplus",
                        "extern \"C\"",
                        "#endif", proto])
            body.append(proto % function)
        body.append(    "int main (int n, char**v) {")
        if call:
            body.append("  (void)%s();" % function)
        else:
            body.append("  %s;" % function)
        body.append(    "  (void)n; (void)v;")
        body.append(    "  return 0;")
        body.append(    "}")
        body = "\n".join(body) + "\n"
        ok = self.try_link(body, headers, include_dirs,
                           libraries, library_dirs, lang=lang)
        return ok

    def check_symbol (self, symbol, type="int",
                      headers=None, include_dirs=None,
                      libraries=None, library_dirs=None,
                      decl=0, lang="c"):
        log.info("checking for symbol '%s' ..." % symbol)
        body = []
        if decl:
            body.append("%s %s;" % (type, symbol))
        body.append("int main (int n, char**v) {")
        body.append("  %s s; s = %s; (void)s;" % (type, symbol))
        body.append("  (void)n; (void)v;")
        body.append("  return 0;")
        body.append("}")
        body = "\n".join(body) + "\n"
        ok = self.try_link(body, headers, include_dirs,
                           libraries, library_dirs, lang=lang)
        return ok

    def check_function_call (self, function, args='',
                             headers=None, include_dirs=None,
                             libraries=None, library_dirs=None,
                             lang="c"):
        log.info("checking for function '%s' ..." % function)
        body = []
        body.append("int main (int n, char**v) {")
        body.append("  (void)%s(%s);" % (function, args))
        body.append("  (void)n; (void)v;")
        body.append("  return 0;")
        body.append("}")
        body = "\n".join(body) + "\n"
        ok = self.try_link(body, headers, include_dirs,
                           libraries, library_dirs, lang=lang)
        return ok

    check_hdr  = check_header
    check_lib  = check_library
    check_func = check_function
    check_sym  = check_symbol

    def run(self):
        config = get_configuration(self, verbose=True)

        # test MPI C compiler
        self.compiler = getattr(
            self.compiler, 'compiler_type', self.compiler)
        self._check_compiler()
        configure_compiler(self.compiler, config, lang='c')
        self.try_link(ConfigTest, headers=['mpi.h'], lang='c')

        # test MPI C++ compiler
        self.compiler = getattr(
            self.compiler, 'compiler_type', self.compiler)
        self._check_compiler()
        configure_compiler(self.compiler, config, lang='c++')
        self.try_link(ConfigTest, headers=['mpi.h'], lang='c++')

        # Generate the Makefile
        with open(os.path.join('mpi_sim', MAKEFILE_TEMPLATE), 'r') as f:
            template = f.read()

        include_dirs = ["-I{0}".format(p) for p in
                        config.library_info["include_dirs"] + [get_python_inc()]]
        include_dirs = ' '.join(include_dirs)
        libs = ["-l{0}".format(p) for p in config.library_info["libraries"]]
        libs = ' '.join(libs)
        mpicxx = config.compiler_info["mpicxx"]

        final = template.format(
            defs="", cxx=mpicxx, mpicxx=mpicxx, include_dirs=include_dirs,
            nengo_mpi_libs=libs, nengo_cpp_libs=libs, mpi_sim_libs=libs)

        concrete_makefile = os.path.join('mpi_sim', MAKEFILE_CONFIGURED)
        print("Creating concrete makefile: {0}".format(concrete_makefile))
        with open(concrete_makefile, 'w') as f:
            f.write(final)


class build(_build):
    description = "Build mpi_sim.so shared library and nengo_mpi/nengo_cpp executables."

    user_options = [('target=', None, "Target to build. If not supplied, all targets are built.")]
    user_options = user_options + _build.user_options

    def initialize_options (self):
        _build.initialize_options(self)
        self.target = None

    def finalize_options (self):
        _build.finalize_options(self)
        if self.target is None:
            self.target = 'all'

    def run(self):
        if not os.path.isfile(os.path.join('mpi_sim', MAKEFILE_CONFIGURED)):
            self.run_command('config')

        _build.run(self)

        print("Building mpi_sim.so, nengo_mpi and nengo_cpp.")
        directory = 'mpi_sim'
        run_make(
            self.target, directory,
            assignments={'EXE_DEST': os.path.join("..", self.build_base),
                         'LIB_DEST': os.path.join("..", self.build_base)},
            makefile_name=MAKEFILE_CONFIGURED)

class install_mpi(Command):
    user_options = []

    def initialize_options (self):
        pass

    def finalize_options (self):
        pass

    def run(self):
        build_base = self.get_finalized_command('build').build_base
        install = self.get_finalized_command('install')
        install_scripts = install.install_scripts
        install_lib = install.install_lib

        exes = [os.path.join(build_base, f) for f in ['nengo_mpi', 'nengo_cpp']]
        libs = [os.path.join(build_base, 'mpi_sim.so')]
        self.outputs = []

        for exe in exes:
            if os.path.isfile(exe):
                print("Copying {0} to {1}.".format(os.path.basename(exe), install_scripts))
                shutil.copy(exe, install_scripts)
                self.outputs.append(os.path.join(install_scripts, os.path.basename(exe)))
            else:
                print("Not installing {0}, no such file.".format(os.path.basename(exe)))

        for lib in libs:
            if os.path.isfile(lib):
                print("Copying {0} to {1}.".format(os.path.basename(lib), install_lib))
                shutil.copy(lib, install_lib)
                self.outputs.append(os.path.join(install_lib, os.path.basename(lib)))
            else:
                print("Not installing {0}, no such file.".format(os.path.basename(lib)))

    def get_outputs(self):
        if hasattr(self, 'outputs'):
            return self.outputs[:]
        else:
            return []


class install(_install):
    sub_commands = _install.sub_commands[:] + [('install_mpi', lambda *a, **k: True)]

    def run(self):
        _install.run(self)


class develop(_develop):
    def run(self):
        _develop.run(self)
        self.run_command('build')
        self.run_command('install_mpi')


class clean(_clean):
    def run(self):
        print("Cleaning mpi.")
        for o in glob("mpi_sim/*.o"):
            try:
                os.remove(o)
            except:
                pass

        try:
            os.remove(os.path.join('mpi_sim', MAKEFILE_CONFIGURED))
        except:
            pass

        _clean.run(self)


@contextmanager
def cd(path):
    """ cd into dir on __enter__, cd back on exit. """

    old_dir = os.getcwd()
    os.chdir(path)

    try:
        yield
    finally:
        os.chdir(old_dir)


def run_make(target, directory, assignments=None, makefile_name=None):
    """
    Parameters
    ----------
    target: str
        Target for make. Can be empty string.
    directory: str
        Location of directory to build in.
    assignments: dict
        Dictionary giving variable assignments to be passed to make invokation.
    makefile_name: str
        Name of a file to use as the makefile instead of the default.

    """
    with cd(directory):
        pid = os.getpid()

        command = ["make"]
        if target:
            command.append(target)
        if makefile_name:
            command.extend("-f {0}".format(makefile_name).split(' '))

        assignments = assignments or {}
        for k, v in assignments.items():
            command.append("{0}={1}".format(k, v))

        process = None
        try:
            print("Running command: %s" % ' '.join(command))

            process = subprocess.Popen(command)
            stdoutdata, stderrdata = process.communicate()

            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, ' '.join(command), None)

            print("Command complete.")

        finally:
            if isinstance(process, subprocess.Popen):
                try:
                    process.terminate()
                except OSError:
                    pass


def safe_print_file(filename):
    try:
        if os.path.isfile(filename):
            with open(filename, 'r') as f:
                for line in iter(f.readline, ''):
                    print(line)
    except IOError:
        pass


def safe_remove_file(filename):
    try:
        os.remove(filename)
    except OSError:
        pass


def configure_dl(ext, config_cmd):
    log.info("checking for dlopen() availability ...")

    ok = config_cmd.check_header('dlfcn.h')
    if ok:
        ext.define_macros += [('HAVE_DLFCN_H', 1)]

    ok = config_cmd.check_library('dl')
    if ok:
        ext.libraries += ['dl']

    ok = config_cmd.check_function('dlopen', libraries=['dl'], decl=1, call=1)
    if ok:
        ext.define_macros += [('HAVE_DLOPEN', 1)]


def run_setup():
    """ Call setup(*args, **kargs) """
    setup_args = metadata.copy()
    setup_args['zip_safe'] = False

    setup(packages=find_packages(),
          cmdclass={'config': config,
                    'build': build,
                    'install': install,
                    'develop': develop,
                    'install_mpi': install_mpi,
                    'clean': clean},
          **setup_args)


def main():
    run_setup()


if __name__ == '__main__':
    main()
