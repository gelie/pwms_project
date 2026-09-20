# gunicorn_config.py
import multiprocessing

# Bind to localhost TCP (stable with the current SELinux policy on this host)
bind = "127.0.0.1:8000"

# Number of worker processes
workers = multiprocessing.cpu_count() * 2 + 1

# Worker class (sync is default, good for most cases)
worker_class = "sync"

# Timeout for workers (30 seconds)
timeout = 30

# Access log file
accesslog = "/var/log/pwms/gunicorn/access.log"

# Error log file
errorlog = "/var/log/pwms/gunicorn/error.log"

# Log level
loglevel = "info"

# Daemonize (run in background)
daemon = False
