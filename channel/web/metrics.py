import time
import web
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Define Prometheus metrics
USER_AUTH_TOTAL = Counter(
    'user_auth_total', 
    'Total user registrations and logins', 
    ['type'] # 'register', 'login', 'invite_code_invalid', etc
)

HTTP_REQUESTS_TOTAL = Counter(
    'http_requests_total', 
    'Total HTTP requests', 
    ['method', 'endpoint', 'status']
)

HTTP_REQUEST_DURATION = Histogram(
    'http_request_duration_seconds', 
    'HTTP request latency', 
    ['method', 'endpoint']
)

DIARY_GENERATION_TOTAL = Counter(
    'diary_generation_total',
    'Completed diary generation jobs',
    ['result', 'mode']
)

DIARY_GENERATION_DURATION = Histogram(
    'diary_generation_duration_seconds',
    'End-to-end diary generation latency',
    ['result', 'mode']
)

DIARY_IMAGE_GENERATION_TOTAL = Counter(
    'diary_image_generation_total',
    'Completed diary image generation attempts',
    ['result']
)

def metrics_processor(handler):
    """Middleware to track request counts and durations."""
    start_time = time.time()
    # Safely get properties
    try:
        endpoint = web.ctx.path
        method = web.ctx.method
    except Exception:
        endpoint = "unknown"
        method = "unknown"
        
    status = '200'
    
    try:
        result = handler()
        # Retrieve status if it was set
        if hasattr(web.ctx, 'status'):
            status = str(web.ctx.status).split(' ')[0]
        return result
    except Exception as error:
        if isinstance(error, web.HTTPError):
            status = str(getattr(web.ctx, 'status', '500')).split(' ')[0]
        else:
            status = '500'
        raise
    finally:
        duration = time.time() - start_time
        # Record metrics only if we resolved the endpoint
        if endpoint != "unknown":
            HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status=status).inc()
            HTTP_REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)

class MetricsHandler:
    def GET(self):
        """Expose /metrics for Prometheus scraping."""
        web.header('Content-Type', CONTENT_TYPE_LATEST)
        return generate_latest()
