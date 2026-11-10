#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    const char *home = "/home/dune/server/Engine/Binaries/ThirdParty/Python3/Linux";
    const char *library = "/home/dune/server/Engine/Binaries/ThirdParty/Python3/Linux/lib/libpython3.9.so.1.0";
    void *handle;
    void (*initialize)(void);
    int (*run)(const char *, void *);
    int (*finalize)(void);

    if (setenv("PYTHONHOME", home, 1) ||
        setenv("PYTHONPATH", "/home/dune/server/Engine/Binaries/ThirdParty/Python3/Linux/lib/python3.9/site-packages", 1)) {
        perror("setenv");
        return 1;
    }
    handle = dlopen(library, RTLD_NOW | RTLD_GLOBAL);
    if (!handle) {
        fprintf(stderr, "embedded Python library: %s\n", dlerror());
        return 1;
    }
    initialize = dlsym(handle, "Py_Initialize");
    run = dlsym(handle, "PyRun_SimpleStringFlags");
    finalize = dlsym(handle, "Py_FinalizeEx");
    if (!initialize || !run || !finalize) {
        fprintf(stderr, "embedded Python symbols are missing\n");
        return 1;
    }
    initialize();
    int result = run("import sys, setuptools, pkg_resources\n"
                     "assert sys.version_info[:2] == (3, 9)\n"
                     "assert setuptools.__version__ == '80.10.2'\n"
                     "from setuptools import Distribution\n"
                     "assert Distribution({'name': 'dune-qualification'}).get_name() == 'dune-qualification'\n",
                     NULL);
    if (finalize() != 0 || result != 0) {
        fprintf(stderr, "embedded Python packaging compatibility failed\n");
        return 1;
    }
    puts("embedded Python 3.9 setuptools 80.10.2 compatibility passed");
    return 0;
}
