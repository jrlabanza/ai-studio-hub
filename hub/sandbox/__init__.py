"""Sandbox: people upload an idea, a script, a tool or a website they built with an AI, change it in the browser and
publish it under a link of its own (``http://<hub>:7910/<name>``).

    intake.py   what may be uploaded: paths, files, folders, zips (bombs, traversal, symlinks), the kind of project
    store.py    the records (SQLite: projects, immutable versions, reserved links, append-only events, view counts)
                and the files on disk (data/sandbox/)
    routes.py   the hub API (/api/sandbox/...) the shell's Sandbox page uses
    serve.py    the content server on its own port - the only place uploaded files are served, always sandboxed

Uploaded code is untrusted: it never runs on the hub's origin or a studio's, see serve.py for how it is isolated.
"""
