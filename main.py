from flask import Flask
import quart
from quart_discord import DiscordOAuth2Session, requires_authorization, Unauthorized
import quart_rate_limiter
from quart_rate_limiter.redis_store import RedisStore
from quart_cors import cors
import json
import asyncpg
import asyncio
import secrets
import uvloop
import sentry_sdk
from sentry_sdk.integrations.quart import QuartIntegration
import datetime

from blueprints.v1 import v1_api

# used between nginx and hypercorn ONLY
import os
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "true"

with open("config.json", "r") as f:
    config = json.load(f)

if config.get("SENTRY_DSN"):
    sentry_sdk.init(dsn=config["SENTRY_DSN"],
                    integrations=[QuartIntegration(transaction_style="url")],
                    traces_sample_rate=1.0,
                    send_default_pii=True)
app = quart.Quart(__name__)
app.register_blueprint(v1_api)
app = cors(app, allow_origin=config.get("CORS_ORIGIN"))

if config.get("REDIS_URI"):
    app.rate_limiter = quart_rate_limiter.RateLimiter(app, store=RedisStore(config["REDIS_URI"]))
else:
    app.rate_limiter = quart_rate_limiter.RateLimiter(app)

app.token_cache = dict()
app.usage_cache = dict()
app.tmp_usage = dict()
app.db = None

async def init_postgres():
    if config.get("POSTGRES_URI"):
            app.db = await asyncpg.create_pool(config["POSTGRES_URI"])
            async with app.db.acquire() as conn:
                await conn.execute("CREATE TABLE IF NOT EXISTS tokens (token TEXT, id BIGINT, email TEXT);")
                await conn.execute("CREATE TABLE IF NOT EXISTS usage (endpoint TEXT, id BIGINT, count INTEGER);")
                for entry in (await conn.fetch("SELECT * FROM tokens;")):
                    app.token_cache[entry.get("id")] = entry.get("token")
    else:
        app.token_cache[246938839720001536] = "TESTING_PURPOSES"

def human_format(num):
    num = float("{:.3g}".format(num))
    magnitude = 0
    while abs(num) >= 1000:
        magnitude += 1
        num /= 1000.0
    return "{}{}".format("{:f}".format(num).rstrip("0").rstrip("."), ["", "K", "M", "B", "T"][magnitude])

async def commit_usage_data():
    try:
        while True:
            await asyncio.sleep(1800)
            if app.usage_cache:
                async with quart.current_app.db.acquire() as connection:
                    async with connection.transaction():
                        entries = []
                        to_del = []
                        current = await connection.fetch("SELECT * FROM usage;")
                        for usr in app.usage_cache:
                            for endpi in app.usage_cache[usr].keys():
                                curr_ind = 0
                                for row in current:
                                    if (row.get("endpoint") == endpi) and (row.get("id") == usr):
                                        curr_ind = row.get("count")
                                        to_del.append((row.get("endpoint"), row.get("id")))
                                entries.append((endpi, usr, app.usage_cache[usr][endpi]+curr_ind))
                        await connection.executemany("DELETE FROM usage WHERE endpoint=$1 AND id=$2;", to_del)
                        await connection.executemany("INSERT INTO usage VALUES ($1, $2, $3);", entries)
                        app.logger.warn(await connection.fetch("SELECT * FROM usage;"))
                app.usage_cache = dict()
    except:
        pass

async def clear_usage_temp():
    try:
        while True:
            await asyncio.sleep(1800)
            app.tmp_usage = {}
    except asyncio.CancelledError:
        pass

@app.before_serving
async def handle_tasks():
    app.add_background_task(init_postgres)
    app.add_background_task(commit_usage_data)
    app.add_background_task(clear_usage_temp)

@app.after_serving
async def cleanup_tasks():
    for tsk in app.background_tasks:
        tsk.cancel()

async def gen_token(user):
    if not app.db:
        return app.token_cache.get(user.id)
    token = secrets.token_urlsafe(15)
    async with app.db.acquire() as connection:
            async with connection.transaction():
                await connection.execute("DELETE FROM tokens WHERE id = $1;", user.id)
                await connection.execute("INSERT INTO tokens VALUES ($1, $2, $3);", token, user.id, user.email)
    app.token_cache[user.id] = token
    return token

app.gen_token = gen_token

# asyncio.get_event_loop().run_until_complete(init_postgres())
# app.add_background_task(init_postgres())

for k in config.keys():
    if k.startswith("APP_"):
        app.config[k.lstrip("APP_")] = config[k]

discord = DiscordOAuth2Session(app)
app.discord = discord

@app.before_request
async def before_request_sentry():
    ipa = quart.request.headers.get("X-Forwarded-For")
    user_data = {"ip_address": ipa}
    if await discord.authorized:
        user = await discord.fetch_user()
        user_data.update({"id": user.id, "username": str(user), "email": user.email})

    sentry_sdk.set_user(user_data)
    sentry_sdk.set_tag("User-Agent", quart.request.headers.get("User-Agent"))
    # print(app.usage_cache)

@app.errorhandler(Unauthorized)
async def redirect_unauthorized(e):
    return await discord.create_session(scope=["identify", "email"])

@app.route("/")
async def index():
    user = None
    if await discord.authorized:
        user = await discord.fetch_user()
    return await quart.render_template("index.html", user=user)

@app.route("/login")
async def login():
    return await discord.create_session(scope=["identify", "email"])

@app.route("/callback")
async def callback():
    await discord.callback()
    return quart.redirect(quart.url_for("index"))

async def pull_usage(user):
    if not app.db:
        return
    if app.tmp_usage.get(user.id):
        return app.tmp_usage[user.id]
    async with app.db.acquire() as connection:
        res = await connection.fetch("SELECT endpoint, count FROM usage WHERE id=$1;", user.id)
        use = [(record["endpoint"], record["count"]) for record in res]
        # all_endpoints = set(x.get("endpoint") for x in res)
        # use = {endp: sum(rec["endpoint"] == endp for rec in res) for endp in all_endpoints}
        app.tmp_usage[user.id] = use
        return use

@app.route("/token")
@requires_authorization
async def token_route():
    user = await discord.fetch_user()
    token = app.token_cache.get(user.id)
    # async with quart.current_app.db.acquire() as connection:
    #     token = (await connection.fetchrow("SELECT token FROM tokens WHERE id = $1", user.id)).get("token")
    if not token:
        token = await app.gen_token(user)
    us_data = await pull_usage(user)
    return await quart.render_template("tokenpage.html", token=token, usage_data=us_data, rqused=sum(ent[1] for ent in us_data), rqtotal=100)

@app.route("/demo/<end>")
@requires_authorization
async def demo(end):
    return f"{quart.request.url_root}{end}?{quart.request.query_string.decode()}".rstrip("?")

if __name__ == "__main__":
    uvloop.install()
    app.run(host="localhost", port=7777, debug=True)