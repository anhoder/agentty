#include "data-types.h"
#if __has_include(<utmpx.h>)
#include <utmpx.h>
#ifdef __APPLE__
#include <paths.h>
#include <sys/stat.h>
#else
#include <signal.h>
#endif

static bool
session_is_active(const struct utmpx *ut) {
#ifdef __APPLE__
    // macOS who checks the terminal device, not the recorded PID.
    char path[sizeof(_PATH_DEV) + sizeof(ut->ut_line)];
    snprintf(path, sizeof(path), "%s%.*s", _PATH_DEV, (int)sizeof(ut->ut_line), ut->ut_line);
    struct stat st;
    return stat(path, &st) == 0;
#else
    pid_t pid = ut->ut_pid;
    if (pid < 1) return false;
    if (kill(pid, 0) >= 0) return true;
    return errno != ESRCH;
#endif
}

static PyObject *
num_users(PyObject *const self UNUSED, PyObject *const args UNUSED) {
    size_t users = 0;
    struct utmpx *ut;
    Py_BEGIN_ALLOW_THREADS setutxent();
    while ((ut = getutxent())) {
        if (ut->ut_type == USER_PROCESS && ut->ut_user[0] && session_is_active(ut)) users++;
    }
    endutxent();
    Py_END_ALLOW_THREADS return PyLong_FromSize_t(users);
}
#else
static PyObject *
num_users(PyObject *const self UNUSED, PyObject *const args UNUSED) {
    PyErr_SetString(PyExc_RuntimeError, "Counting the number of users is not supported");
    return NULL;
}
#endif

static PyMethodDef methods[] = {
    {"num_users", num_users, METH_NOARGS, "Get the number of users using UTMP data"},
    {NULL, NULL, 0, NULL},
};

bool
init_utmp(PyObject *module) {
    return PyModule_AddFunctions(module, methods) == 0;
}
