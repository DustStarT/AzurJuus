"""Machine wall time for context; elapsed activity time stays monotonic."""
import time
from datetime import datetime, timezone


def snapshot():
    stamp=time.time()
    local=datetime.fromtimestamp(stamp,timezone.utc).astimezone()
    return {'unixSeconds':stamp,'local':local.isoformat(timespec='seconds'),
        'date':local.date().isoformat(),'hour':local.hour,'weekday':local.isoweekday(),
        'timezone':local.tzname(),'utcOffsetSeconds':int(local.utcoffset().total_seconds()),
        'source':'machine-clock'}
