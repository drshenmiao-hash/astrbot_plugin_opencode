"""Apply Linux process limits before exec, without preexec_fn in a threaded host."""

import os
import resource
import sys

if __name__ == "__main__":
    for limit, maximum in (
        (resource.RLIMIT_CPU, 3),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_NOFILE, 128),
        (resource.RLIMIT_FSIZE, 16 * 1024 * 1024),
    ):
        _, hard = resource.getrlimit(limit)
        bound = maximum if hard == resource.RLIM_INFINITY else min(hard, maximum)
        resource.setrlimit(limit, (bound, bound))
    os.execv(sys.argv[1], sys.argv[1:])
