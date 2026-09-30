# Compose overrides

One optional file per studio, named after its compose service (`ai.tool` label). When the hub
starts a studio's container on Linux it runs

    docker compose -f <studio>/linux/compose.yml -f hub/compose/<service>.yml up ...

so the studio's own packaging stays untouched and the hub only adds what it needs: mostly
"start idle" switches, because the hub - not the container's boot - decides when a model is
loaded onto the GPU. Video Studio needs no file (the hub sets `engine_autostart: false` in its
`data/settings.json`, see tools.py); Music Studio and Forge already start without
touching the GPU.
