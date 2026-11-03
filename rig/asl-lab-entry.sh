#!/bin/sh
# SPDX-License-Identifier: MIT
# asl-lab container entry: render the private config for this host, start the
# control agent (and dnsmasq on the registrar), run asterisk in the foreground.
#   env DVT_NET             rig /24 prefix (required)
#   env DVT_ASL_ROLE        node | registrar | tap | node-ext (required; tap = agent only, in another container's netns;
#                           node-ext = the external node of profile asl-ext: real node number, registers with the real
#                           registry, password read from the mounted file /run/dvt-secret/node.pw, outbound only)
#   env DVT_ASL_NODE        private test node number (role node)
#   env DVT_ASL_USRP_TX/RX  chan_usrp ports (role node; nothing listens there)
#   env DVT_ASL_IAX_PORT    IAX2 UDP port (default 4569)
#   env DVT_ASL_DOMAIN      lab node-name zone (default nodes.asl.test)
#   env DVT_ASL_OWN_IP      this host's rig address (registrar: dnsmasq listens there)
#   env DVT_ASL_ZONE        registrar: "<node>:<ip>:<port> ..." for the DNS zone and the registrar users
#   env DVT_ASL_ACCT_SECRET fixture secret of the account users (default test-secret-not-real)
#   env DVT_ASL_REG_SECRET  fixture secret of the registrar users (default test-secret-not-real)
# Secrets here are fixtures for a private lab, not credentials of any service.
set -eu

: "${DVT_NET:?DVT_NET is required}"
role="${DVT_ASL_ROLE:?DVT_ASL_ROLE is required}"
port="${DVT_ASL_IAX_PORT:-4569}"
domain="${DVT_ASL_DOMAIN:-nodes.asl.test}"
acct="${DVT_ASL_ACCT_SECRET:-test-secret-not-real}"
regsec="${DVT_ASL_REG_SECRET:-test-secret-not-real}"

case "$port" in ''|*[!0-9]*) echo "dvt-asl-lab: DVT_ASL_IAX_PORT must be digits" >&2; exit 2 ;; esac
case "$domain" in ''|*[!A-Za-z0-9.-]*) echo "dvt-asl-lab: DVT_ASL_DOMAIN has odd characters" >&2; exit 2 ;; esac
case "$acct$regsec" in *[!A-Za-z0-9_.-]*) echo "dvt-asl-lab: secrets must be [A-Za-z0-9_.-]" >&2; exit 2 ;; esac

src=/etc/asterisk/dvt-lab
etc=/etc/asterisk
rec=/var/spool/asterisk/dvt-rec

case "$role" in
  tap)
    # shares the network namespace of another container (asl-edge: the SUT): only the agent, for tcpdump there
    mkdir -p /var/lib/dvt-lab/pcap
    echo "dvt-asl-lab: role=tap"
    exec python3 /usr/local/sbin/dvt-asl-lab-agent
    ;;
  node)
    node="${DVT_ASL_NODE:?DVT_ASL_NODE is required}"
    case "$node" in ''|*[!0-9]*) echo "dvt-asl-lab: DVT_ASL_NODE must be digits" >&2; exit 2 ;; esac
    utx="${DVT_ASL_USRP_TX:?}"; urx="${DVT_ASL_USRP_RX:?}"
    sed -e "s/@PORT@/$port/g" -e "s/@ACCT_SECRET@/$acct/g" "$src/iax-node.conf.in" > "$etc/iax.conf"
    sed -e "s/@NODE@/$node/g" -e "s/@PORT@/$port/g" -e "s/@DOMAIN@/$domain/g" \
        -e "s/@USRP_TX@/$utx/g" -e "s/@USRP_RX@/$urx/g" "$src/rpt.conf.in" > "$etc/rpt.conf"
    export DVT_NODE="$node"
    ;;
  node-ext)
    # profile asl-ext: the real node number, outbound only. The registration password is a FILE (mounted read-only);
    # it is copied into the Asterisk config here with mode 0600 and never goes through a variable, an argument,
    # an environment variable or a log line.
    node="${DVT_ASL_NODE:?DVT_ASL_NODE is required}"
    case "$node" in ''|*[!0-9]*) echo "dvt-asl-lab: DVT_ASL_NODE must be digits" >&2; exit 2 ;; esac
    utx="${DVT_ASL_USRP_TX:?}"; urx="${DVT_ASL_USRP_RX:?}"
    reg="${DVT_ASL_EXT_REGISTRY:-register.allstarlink.org}"
    case "$reg" in ''|*[!A-Za-z0-9.-]*) echo "dvt-asl-lab: DVT_ASL_EXT_REGISTRY has odd characters" >&2; exit 2 ;; esac
    pwf=/run/dvt-secret/node.pw
    [ -f "$pwf" ] && [ -r "$pwf" ] && [ -s "$pwf" ] || { echo "dvt-asl-lab: node-ext needs the password file $pwf" >&2; exit 2; }
    sed -e "s/@PORT@/$port/g" "$src/ext/iax.conf.in" > "$etc/iax.conf"
    sed -e "s/@NODE@/$node/g" -e "s/@PORT@/$port/g" \
        -e "s/@USRP_TX@/$utx/g" -e "s/@USRP_RX@/$urx/g" "$src/ext/rpt.conf.in" > "$etc/rpt.conf"
    cp "$src/ext/extensions.conf" "$etc/extensions.conf"
    # The live registration file starts with no entry: the node does NOT register when Asterisk starts. The
    # staged copy (root only, mode 0600) holds the real line; the agent's POST /ext/register moves it into place
    # and reloads the module, so the case chooses the moment (capture running first) and can withdraw it again.
    ( umask 077
      printf '[general]\nregister_interval = 180\n\n[registrations]\n' > "$etc/rpt_http_registrations.conf"
      {
        printf '[general]\nregister_interval = 180\n\n[registrations]\n'
        printf 'register => %s:' "$node"
        tr -d '\r\n' < "$pwf"
        printf '@%s\n' "$reg"
      } > /etc/asterisk/dvt-ext-registration.staged )
    chown root:root /etc/asterisk/dvt-ext-registration.staged
    chown asterisk:asterisk "$etc/rpt_http_registrations.conf" "$etc/rpt.conf" "$etc/extensions.conf" "$etc/iax.conf"
    chmod 600 "$etc/rpt_http_registrations.conf" /etc/asterisk/dvt-ext-registration.staged
    chmod 640 "$etc/iax.conf"
    # chan_usrp on; nothing else changed: res_rpt_http_registrations stays loaded (it is "require" in the ASL3 default)
    sed -i -E -e 's/^(noload|load)( *= *chan_usrp\.so)/require\2/' "$etc/modules.conf"
    mkdir -p /var/run/asterisk /var/lib/dvt-lab/pcap
    chown asterisk:asterisk /var/run/asterisk
    echo "dvt-asl-lab: role=$role node=$node registry=$reg $(tr '\n' ' ' < "$etc/dvt-versions.txt")"
    export DVT_NODE="$node" DVT_ASL_EXT=1
    python3 /usr/local/sbin/dvt-asl-lab-agent &
    exec asterisk -f -n -U asterisk -G asterisk -vvv
    ;;
  registrar)
    own="${DVT_ASL_OWN_IP:?DVT_ASL_OWN_IP is required}"
    zone="${DVT_ASL_ZONE:?DVT_ASL_ZONE is required}"
    sed -e "s/@PORT@/$port/g" "$src/iax-registrar.conf.in" > "$etc/iax.conf"
    cp "$src/rpt-registrar.conf" "$etc/rpt.conf"
    dns=/etc/dnsmasq-lab.conf
    {
      echo "# generated by dvt-asl-lab-entry"
      echo "port=53"
      echo "listen-address=$own"
      echo "bind-interfaces"
      echo "no-resolv"
      echo "no-hosts"
      echo "user=root"
      echo "log-queries"
      echo "log-facility=-"
      echo "local-ttl=30"
      echo "local=/$domain/"
      echo "host-record=register.$domain,$own"
    } > "$dns"
    for ent in $zone; do
      n="${ent%%:*}"; rest="${ent#*:}"; ip="${rest%%:*}"; p="${rest#*:}"
      case "$n$ip$p" in *[!0-9.]*|'') echo "dvt-asl-lab: bad DVT_ASL_ZONE entry '$ent'" >&2; exit 2 ;; esac
      # ASL shape: SRV _iax._udp.<node>.<domain> -> port + target <node>.<domain>; A <node>.<domain>; TXT NN/IP/PT
      echo "srv-host=_iax._udp.$n.$domain,$n.$domain,$p,0,0" >> "$dns"
      echo "host-record=$n.$domain,$ip" >> "$dns"
      echo "txt-record=$n.$domain,\"NN=$n\",\"IP=$ip\",\"PT=$p\"" >> "$dns"
      {
        echo
        echo "[$n]"
        echo "type = friend"
        echo "host = dynamic"
        echo "auth = md5"
        echo "secret = $regsec"
        echo "context = radio-local"
        echo "disallow = all"
        echo "allow = ulaw"
        echo "requirecalltoken = no"
      } >> "$etc/iax.conf"
    done
    ;;
  *) echo "dvt-asl-lab: DVT_ASL_ROLE must be node, registrar, tap or node-ext" >&2; exit 2 ;;
esac

cp "$src/extensions.conf" "$etc/extensions.conf"
: > "$etc/dvt-register.conf"
chown asterisk:asterisk "$etc/rpt.conf" "$etc/extensions.conf" "$etc/iax.conf" "$etc/dvt-register.conf"
chmod 640 "$etc/iax.conf" "$etc/dvt-register.conf"

# modules.conf: chan_usrp on; USB radio drivers and node registration over
# HTTPS (res_rpt_http_registrations) off, so nothing can reach a portal.
sed -i -E \
  -e 's/^(noload|load)( *= *chan_usrp\.so)/require\2/' \
  -e 's/^(load|require)( *= *(chan_simpleusb|res_usbradio|res_rpt_http_registrations)\.so)/noload\2/' \
  "$etc/modules.conf"
# the scenarios record what a link receives (MixMonitor r()) and place calls as an account
# user (channel originate); ASL3 loads modules by name only (autoload = no)
sed -i -e '/^autoload/a load = app_mixmonitor.so' -e '/^autoload/a load = res_clioriginate.so' "$etc/modules.conf"
: > "$etc/rpt_http_registrations.conf"

mkdir -p /var/run/asterisk "$rec" /var/lib/dvt-lab/pcap /var/lib/asterisk/sounds/dvt
chown asterisk:asterisk /var/run/asterisk "$rec" /var/lib/asterisk/sounds/dvt

echo "dvt-asl-lab: role=$role $(tr '\n' ' ' < "$etc/dvt-versions.txt")"
[ "$role" != registrar ] || dnsmasq --conf-file=/etc/dnsmasq-lab.conf --keep-in-foreground &
# plain-HTTP stand-in for the ASL registrar (DV-Router's HTTPS/JSON registration with insecure = true)
[ "$role" != registrar ] || mkdir -p /var/lib/dvt-lab/register
[ "$role" != registrar ] || python3 /usr/local/sbin/dvt-asl-lab-registrar &
python3 /usr/local/sbin/dvt-asl-lab-agent &
exec asterisk -f -n -U asterisk -G asterisk -vvv
