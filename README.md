# StarTV EPG

Genera la guia de programacion (EPG) de StarTV en formato **XMLTV** (`epg.xml`)
y la publica automaticamente en **GitHub Pages** mediante GitHub Actions.

## Como funciona

1. `channels_resumen.json` contiene la lista de canales (`contentId`, `number`, `title`).
2. `generate_epg.py` consulta la API de StarTV para cada canal y arma `epg.xml`
   (7 dias de programacion por defecto).
3. El workflow `.github/workflows/epg.yml` corre cada 12 h, regenera el EPG y lo
   publica en GitHub Pages.

URL final del EPG (una vez configurado Pages):

```
https://<tu-usuario>.github.io/<tu-repo>/epg.xml
https://<tu-usuario>.github.io/<tu-repo>/epg.xml.gz
```

## Configuracion en GitHub (una sola vez)

1. **Sube este repo** a GitHub.
2. **Activa GitHub Pages**: `Settings -> Pages -> Build and deployment -> Source: GitHub Actions`.
3. **Crea el secret del token**: `Settings -> Secrets and variables -> Actions -> New repository secret`
   - Name: `STARTV_TOKEN`
   - Value: el JWT (con o sin el prefijo `Bearer `).
4. Ejecuta el workflow a mano la primera vez: pestania **Actions -> Generar EPG StarTV -> Run workflow**.

### De donde sale el token

El token es un JWT emitido por la plataforma OTT de StarTV (MediaKind/M10, login via
Cognito). Caduca (aprox. mensual). Para obtenerlo:

1. Entra a `https://edgelb.stargroup.com.mx/web/startv/` con tu cuenta.
2. Abre DevTools (F12) -> pestania **Network**.
3. Filtra por `epgcache` o `xtv-ws-client`.
4. Abre cualquier peticion y copia el header `Authorization` (todo lo que va despues de `Bearer `, o el valor completo).
5. Pega ese valor en el secret `STARTV_TOKEN`.

Cuando el EPG deje de actualizarse, repite estos pasos: el workflow avisa en el log
cuando el token esta por caducar o ya caduco.

## Uso local (opcional)

```bash
pip install -r requirements.txt

# Linux/Mac
export STARTV_TOKEN="<tu-token>"
python generate_epg.py

# Windows PowerShell
$env:STARTV_TOKEN = "<tu-token>"
python generate_epg.py
```

Salida en `public/epg.xml`.

## Variables de entorno

| Variable          | Default                                  | Descripcion                              |
|-------------------|------------------------------------------|------------------------------------------|
| `STARTV_TOKEN`    | (obligatorio)                            | JWT de StarTV.                           |
| `STARTV_APP_ID`   | `d47a651b-3842-46b1-9f2f-ac978a254b88`   | App ID de la plataforma.                 |
| `STARTV_LINEUP_ID`| `2342`                                   | Lineup / region.                         |
| `EPG_DAYS`        | `7`                                      | Dias de programacion a descargar.        |
| `EPG_OUTPUT`      | `public/epg.xml`                         | Ruta del XML de salida.                  |
| `EPG_CHANNELS`    | `channels_resumen.json`                  | Ruta del JSON de canales.                |
| `EPG_WORKERS`     | `6`                                      | Descargas en paralelo.                   |
| `EPG_PROXY`       | (vacio)                                  | Proxy opcional (`socks5://host:1080`).   |

## Notas

- El generador descarga los canales en paralelo (6 a la vez) con reintentos automaticos.
- Si **todos** los canales fallan, el job falla (señal de token caducado/invalido).
- Las horas se escriben en hora de Mexico (UTC-6).
