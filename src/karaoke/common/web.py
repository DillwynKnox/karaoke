"""Flask helpers shared by the hub and the nodes, free of any heavy imports."""

import io
import re
import threading

import segno
from flask import Flask, Response, abort

from karaoke import config
from karaoke.common.invite import invite_url

# A song key is its ISRC or 'deezer-<id>'; it ends up in file paths, so it stays strict.
_SONG_KEY = re.compile(r'^[A-Za-z0-9-]+$')


def make_app(name):
    return Flask(name, template_folder=config.TEMPLATES_DIR, static_folder=config.STATIC_DIR)


def check_key(key):
    if not _SONG_KEY.match(key):
        abort(404)


def guest_qr():
    """QR code of the public guest page with the invite code that is valid right now."""
    url = invite_url()
    if not url:
        abort(404)
    # A standalone SVG: the inline flavour has no xmlns, so browsers do not render it as an image.
    svg = io.BytesIO()
    segno.make(url, error='m').save(svg, kind='svg', scale=6, dark='#000', light='#fff', border=3, xmldecl=False)
    # The code changes, so the browser must not keep an old one.
    return Response(svg.getvalue(), mimetype='image/svg+xml', headers={'Cache-Control': 'no-store'})


def shown_host():
    return 'localhost' if config.HTTP_HOST in ('127.0.0.1', '0.0.0.0') else config.HTTP_HOST


def serve(app, port, background=False):
    kwargs = {'host': config.HTTP_HOST, 'port': port, 'threaded': True}
    if background:
        threading.Thread(target=app.run, kwargs=kwargs, daemon=True).start()
    else:
        app.run(**kwargs)
