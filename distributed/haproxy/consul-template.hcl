consul {
  address = "127.0.0.1:8500"
}

reload_signal = "SIGHUP"

template {
  source      = "/etc/haproxy/haproxy.cfg.ctmpl"
  destination = "/etc/haproxy/haproxy.cfg"
  command     = "systemctl reload haproxy"
}
