from slowapi import Limiter
from slowapi.util import get_remote_address

# Shared limiter instance — mounted on app.state.limiter in main.py.
# Webhook endpoints use 200 req/min per IP; legitimate senders (GitHub, GitLab,
# Atlassian, Teams) fire well below this; sustained flooding is blocked.
limiter = Limiter(key_func=get_remote_address)
