# btproxy

An Alpine image containing BlueZ's `btproxy` and `btmgmt`. `btproxy` forwards a Bluetooth controller over TCP and creates a virtual controller on the client. The image builds `btproxy` from the official BlueZ 5.87 source.

The default command listens on `127.0.0.1:45550` using controller `hci0`. Set a reachable listen address to accept connections from another machine.

## Server

The host needs a Bluetooth controller, and the container needs access to the host network and Bluetooth sockets. Power off the controller before `btproxy` opens its exclusive HCI user channel. If another service uses the controller, release it first. Replace `SERVER_IP` with the server's reachable IPv4 address:

```sh
docker run --rm --network host --privileged \
  --entrypoint /usr/bin/btmgmt \
  ghcr.io/hyec/my-docker-images/btproxy:latest \
  --index hci0 power off

docker run --rm --network host --privileged \
  ghcr.io/hyec/my-docker-images/btproxy:latest \
  --listen=SERVER_IP --port=45550 --index=0
```

For another controller, change both indexes (for example, `hci1` and `--index=1`).

## Client

Load `hci_vhci` on the client host, then connect with `btproxy`. The same image can run the client:

```sh
sudo modprobe hci_vhci

docker run --rm --network host --privileged \
  ghcr.io/hyec/my-docker-images/btproxy:latest \
  --connect=SERVER_IP --port=45550
```

In another terminal on the client host, check that BlueZ sees the virtual controller:

```sh
bluetoothctl list
```

One client can own the selected controller at a time. The server cannot use that controller locally while it is proxied. `btproxy` provides no authentication or encryption; restrict the TCP port to trusted clients or carry the connection over a VPN.
