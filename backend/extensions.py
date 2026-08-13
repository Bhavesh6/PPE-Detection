from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
jwt = JWTManager()

# In-memory storage — fine for the single gunicorn worker this app actually
# runs with (Render's free tier can't afford more than one anyway; see
# render.yaml). Multiple workers would each keep their own counts and the
# limits would silently become worker_count times looser — worth revisiting
# with a shared store (Redis) only if that ever changes.
limiter = Limiter(key_func=get_remote_address)
