# In-memory fan-out requires EXACTLY one process and one App Service instance.
bind = '0.0.0.0:8000'
workers = 1
worker_class = 'gthread'
threads = 16
# At most 8 SSE clients, leaving threads available for normal requests.
timeout = 120
graceful_timeout = 15
preload_app = False
accesslog = '-'
errorlog = '-'


def post_worker_init(worker):
    from app import consumer
    consumer.start()


def worker_exit(server, worker):
    from app import consumer
    consumer.stop()
