import os
import signal
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["TEST_CHILD_PID_FILE"]).write_text(str(os.getpid()))
time.sleep(60)
