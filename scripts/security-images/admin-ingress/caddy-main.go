package main

import (
	_ "time/tzdata"

	caddycmd "github.com/caddyserver/caddy/v2/cmd"
	_ "github.com/caddyserver/caddy/v2/modules/standard"
	_ "github.com/porech/caddy-maxmind-geolocation"
)

func main() {
	caddycmd.Main()
}
