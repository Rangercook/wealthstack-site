#!/bin/sh
set -eu
# Render's newly attached disk can be root-owned. Make only the worker-owned
# directory entries writable, leaving existing videos and database untouched.
# A Render service without an attached disk has an ephemeral filesystem.
# Fail closed until the user attaches the approved 10 GB disk at /data.
if [ "${MPT_REQUIRE_PERSISTENT_DISK:-1}" = "1" ]; then
    python -c 'import os,sys; sys.exit(0 if os.path.ismount("/data") else 65)' || {
        echo 'FATAL: attach the 10 GB persistent disk at /data in Render Dashboard > Disk before starting the worker.' >&2
        exit 65
    }
fi
mkdir -p /data/jobs /data/outputs /data/logs /data/scratch
chown mpt:mpt /data /data/jobs /data/outputs /data/logs /data/scratch
exec gosu mpt "$@"
