# Túnel privado para la guardia H2

El túnel privado H2 ya está activo. La torre inicia una conexión SSH hacia el VPS y reenvía su clasificador local sin exponerlo en Internet. La inferencia escucha solo en `127.0.0.1:18473` en la torre y el puerto `127.0.0.1:19091` existe únicamente en el VPS. No se abrió el router ni el firewall para la torre.

## Torre

En el despliegue actual, la torre usa servicios systemd de usuario con inicio persistente habilitado. La guardia y el túnel corren como `kevingg` con restricciones de systemd; los archivos de unidad publicados son `coffee-house-hackathon2-guard-user.service` y `coffee-house-hackathon2-guard-tunnel-user.service`. La cuenta de inicio persistente ya estaba habilitada. El usuario de túnel remoto en el VPS es independiente y no tiene shell.

```sh
sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin coffee-house-h2-guard
sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin coffee-house-h2-tunnel
```

La unidad de usuario mantiene el entorno Python dedicado probado para T1 y carga el snapshot local `Qwen/Qwen3Guard-Gen-0.6B` fijado a `fada3b2f655b89601929198343c94cd2f64d93cc`. HF Hub y Transformers están en modo sin conexión; el servicio no registra textos de usuario y queda en loopback. La RAM observada durante carga fue de unos 2.5 GB y la carga tardó 1.784 s.

   ```sh
   python3.13 -m venv /opt/coffee-house-hackathon2-guard/.venv
   /opt/coffee-house-hackathon2-guard/.venv/bin/pip install -r /opt/coffee-house-hackathon2-guard/requirements-py313.lock
   ```

Las plantillas de servicio como cuentas de sistema permanecen disponibles si se convierte la demo a cuentas separadas con privilegios administrativos. No se instalaron en la torre en este despliegue; las unidades activas son las variantes `-user.service`.

## VPS

El usuario remoto `coffee-house-h2-tunnel` está restringido con una cuenta sin shell y una clave SSH dedicada. El bloque de `sshd` permite solo reenvío TCP remoto; `authorized_keys` limita la clave al listener `127.0.0.1:19091`. Se validó con `sshd -t` y `sshd -T -C` antes de recargar SSH. El túnel H1 no se modificó y conserva `127.0.0.1:18189`.

## Comprobación y retirada

- En la torre: `systemctl --user status coffee-house-hackathon2-guard.service` y `...-guard-tunnel.service`; la interfaz `/health` debe identificar el snapshot fijado.
- En el VPS: confirma el listener `127.0.0.1:19091` y que el cliente del servicio H2 acepta una clasificación segura. Si la guardia o el túnel cae, el flujo pausa la consulta.
- Antes de cambiar la política de SSH, valida `sshd -t` y los valores efectivos; conserva abierta la sesión administrativa mientras pruebas.
- Para retirar el tramo, detén solo las unidades de guardia/túnel H2 y elimina solo el usuario/clave H2 después de revisar que no mantiene otras funciones.

La verificación del despliegue activo incluye conexión por clave desde la torre, clasificación de una pregunta sintética desde el cliente H2 en el VPS y confirmación de que los dos puertos solo escuchan en loopback.

La política de servidor sigue las opciones oficiales de OpenSSH para `AllowTcpForwarding`, `PermitListen`, `GatewayPorts` y `MaxSessions`; las restricciones de clave usan `restrict`, `port-forwarding` y `permitlisten`. Véanse [sshd_config(5)](https://man.openbsd.org/sshd_config.5), [sshd(8)](https://man.openbsd.org/sshd.8) y [ssh_config(5)](https://man.openbsd.org/ssh_config.5).
