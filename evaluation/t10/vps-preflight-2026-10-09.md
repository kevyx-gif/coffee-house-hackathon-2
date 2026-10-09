# Preflight del VPS para Hackathon 2

Fecha: 2026-10-09. **Solo inspección y validación temporal; no es un despliegue.**

## Plataforma y compatibilidad

- El VPS objetivo corre Debian 12, systemd 252.39 y OpenSSH 9.2.
- Las tres plantillas systemd de H2 pasaron `systemd-analyze verify` contra una raíz temporal con usuarios, carpetas y ejecutables ficticios compatibles con las rutas declaradas. No se creó ninguna cuenta o ruta real.
- El backend fijado y las dependencias de prueba se instalaron en un entorno virtual temporal con el Python 3.11.2 del VPS. `pip check` no encontró dependencias rotas y la suite H2+T1 pasó **93 pruebas y omitió 2**; las omitidas requieren Tesseract. La advertencia informativa de Starlette TestClient/HTTPX también aparece en el entorno local. Se retiró el entorno temporal de 107 MB tras la corrida.
- La política de túnel propuesta se anexó a una copia temporal que incluía el `sshd_config` real y sus inclusiones. `sshd -t` pasó. `sshd -T -C` para el usuario hipotético produjo `allowtcpforwarding remote`, `permitlisten 127.0.0.1:19091`, `gatewayports no`, `maxsessions 0`, `passwordauthentication no`, `permittty no`, `allowagentforwarding no` y `x11forwarding no`.
- Se eliminaron las dos raíces temporales al terminar. No se recargó ni reinició `sshd` o systemd.

## Estado observado

- Hackathon 1 conserva su unidad activa y responde HTTP 200 en su URL pública.
- No existe directorio, servicio systemd ni listener H2 en el VPS; tampoco se modificaron Nginx, firewall, DNS ni rutas públicas.
- Una consulta Meta de solo lectura `GET /{PHONE_NUMBER_ID}?fields=id` devolvió HTTP 401, código 190/subcódigo 463. No se envió ningún mensaje; hay que renovar la autorización.
- La consulta de solo lectura a la lista de modelos de Groq devolvió HTTP 403 `text/plain` desde Windows con la clave configurada y desde el VPS sin credencial. No permite atribuir el resultado a la clave ni validar inferencia. El modelo configurado `qwen/qwen3.8-27b` aparece en la [lista oficial de modelos de Groq](https://console.groq.com/docs/models); no se hizo una solicitud de inferencia.

## Decisión

El VPS tiene compatibilidad de plataforma y la restricción de SSH se puede validar en una configuración temporal. **T9 y T10 siguen abiertas:** no instalar el servicio ni el túnel hasta recuperar acceso válido a Meta/Groq y completar el flujo requerido de WhatsApp y dispositivo. Este preflight no prueba autenticación real del túnel, webhook, mensajes, inferencia, OCR ni recuperación integral.

## Revalidación de la entrega final — 2026-10-09

Se copió temporalmente al VPS únicamente el código saneado, datos de demostración, pruebas y archivos de dependencias del repositorio de entrega. En Debian 12 / Python 3.11.2: `pip check` limpio, **95 pruebas aprobadas y 2 omitidas** por Tesseract, y Ruff aprobó. El entorno y el archivo transferido fueron eliminados. En local, un entorno limpio Python 3.13 obtuvo los mismos 95/2, con Ruff y `compileall` aprobados. H1 continuó activo; H2 sigue sin directorio, unidad o listener.

## Revalidación temporal posterior — 2026-10-09

Tras añadir una prueba vertical sintética de T9, la exportación volvió a instalarse en una carpeta temporal del VPS. Debian 12/Python 3.11.2 pasó `pip check`, **96 pruebas aprobadas y 2 omitidas**, Ruff y `compileall`. La instalación, el archivo y el script de validación temporales se eliminaron y se comprobó su ausencia. H1 siguió activo y la ruta pública respondió HTTP 200. El inventario de systemd no muestra unidades H2. No se instaló ni desplegó Hackathon 2.

## Revalidación posterior al ajuste de modalidad — 2026-10-09

La entrega saneada se instaló de nuevo en una ruta temporal del VPS Debian 12 con Python 3.11.2. `pip check` pasó; la suite H2+T1 obtuvo **100 aprobadas y 2 opcionales omitidas**; Ruff y `compileall` pasaron. La misma exportación obtuvo los mismos resultados en un entorno limpio local Python 3.13. Se eliminaron la carpeta temporal, el entorno virtual, el archivo transferido y el script, y se verificó que ya no existían.

La ruta real de inferencia `POST /chat/completions` de Groq se probó con una solicitud de menú sintética y respondió una herramienta permitida; además, cuatro casos de la evaluación T5 usaron inferencia real con textos sintéticos. La consulta previa `GET /models` devolvió 403, pero no sirve como prueba de disponibilidad de inferencia. La comprobación de solo lectura de Meta todavía devuelve 401/código 190/subcódigo 463; no se envió ningún mensaje. H1 siguió activo y su URL respondió HTTP 200. No hay servicio ni listener H2 en el VPS. **T9 continúa abierta por la autorización Meta y las pruebas de WhatsApp en dispositivos; por la dependencia aprobada, no se desplegó H2.**

## Revalidación después del ajuste de enrutamiento — 2026-10-09

La exportación saneada de **75 archivos** pasó `pip check`, **103 pruebas aprobadas y 2 omitidas** en una instalación limpia local Python 3.13 y en una temporal del VPS Debian 12/Python 3.11.2; Ruff y compilación también pasaron. Se retiró la instalación temporal remota. Después de la prueba, H1 siguió activo y su URL devolvió HTTP 200; no existe unidad H2. El cambio de temperatura/extras quedó cubierto por tests y por la batería Groq sintética 20/20 + 5/5, registrada en `evaluation/t9/`. No se instaló ni se desplegó H2. La autorización de Meta sigue vencida; no se mandaron mensajes.

## Validación temporal de la revisión ampliada — 2026-10-09

La última validación temporal instaló el paquete saneado de 81 archivos en Debian 12/Python 3.11.2: `pip check` quedó limpio; pasaron 104 pruebas, se omitieron 2 que requieren Tesseract, Ruff y compilación. Se eliminó y verificó la ausencia de la carpeta temporal, el archivo transferido y el entorno virtual. No hay unidad ni listener H2 en el VPS, y la página pública de H1 respondió HTTP 200. No se modificó Nginx, firewall, systemd, la base H1 ni la aplicación pública. Después se ejecutó la batería conversacional corregida de 22 mensajes: 22/22 claras, 5/5 ambiguas, sin reintentos ni fallos. Este ensayo no constituye integración real ni despliegue de WhatsApp.

## Revalidación final reproducible — 2026-10-09

La exportación saneada de 81 archivos se extrajo en una carpeta temporal de Debian 12/Python 3.11.2. Con el `PYTHONPATH` documentado, `pip check` pasó, la suite obtuvo **104 aprobadas y 2 omitidas** (integraciones OCR que requieren Tesseract), Ruff y `compileall` pasaron. La limpieza automática se verificó: no quedó carpeta temporal. El servicio H2 sigue ausente y el puerto 18795 cerrado; la web H1 respondió HTTP 200. No se instalaron unidades ni se alteraron proxy, firewall o datos de H1. Esta validación no sustituye el recorrido real de WhatsApp ni cierra T9/T10.

## Revalidación tras incorporar retención automática — 2026-10-09

La exportación saneada actual de 81 archivos, incluida la limpieza automática y la caducidad del outbox, se extrajo en un entorno temporal de Debian 12/Python 3.11.2. `pip check` pasó; H2+T1 obtuvo **106 pruebas aprobadas y 2 omitidas** (OCR/Tesseract opcional); Ruff y `compileall` pasaron. La misma suite local con Python 3.13 tuvo el mismo resultado. Se comprobó que el área de validación temporal quedó limpia. La URL de Hackathon 1 siguió respondiendo HTTP 200; H2 no tiene unidad systemd ni listener en el puerto 18795. No se tocaron proxy, firewall, base ni servicio de H1. La autorización de Meta sigue vencida y aún falta validar WhatsApp desde móvil y computadora, por lo que esto no cierra T9 ni habilita T10/T11.
