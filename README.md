# Maria Speaks English — RSS completo para Feedly

Este proyecto genera un único RSS de **@mariaspeaksenglish** en YouTube e incluye:

- vídeos normales;
- Shorts;
- directos archivados;
- catálogo histórico descubierto por `yt-dlp`;
- actualización automática cada 6 horas;
- relleno histórico por lotes para que Feedly tenga varias oportunidades de indexar vídeos antiguos.

## Puesta en marcha

1. Crea un repositorio **público** nuevo en GitHub, por ejemplo `maria-speaks-rss`.
2. Sube **todo** el contenido de esta carpeta, incluida la carpeta `.github`.
3. En GitHub abre **Actions → Actualizar RSS Maria Speaks English → Run workflow**.
4. Cuando termine, aparecerán `feed.xml`, `catalog.json` y `backfill_state.json` en el repositorio.
5. En Feedly añade esta URL, sustituyendo `TU_USUARIO` y, si procede, el nombre del repositorio:

   `https://raw.githubusercontent.com/TU_USUARIO/maria-speaks-rss/main/feed.xml`

## Cómo funciona el histórico

Feedly conserva los artículos que ya ha indexado, pero no siempre importa de golpe cientos de entradas antiguas de una fuente recién añadida. Por eso el RSS mantiene siempre los **75 elementos más recientes** y expone además **100 elementos históricos** durante 24 horas. Después pasa al siguiente lote.

Con unos pocos cientos de vídeos, el histórico completo debería ir quedando indexado progresivamente en varios días. Los elementos mantienen un `GUID` estable (`youtube:ID`), de modo que no deberían aparecer duplicados cuando vuelvan a entrar en el feed.

Los vídeos de más de 30 días pueden aparecer como **leídos** en Feedly. Para verlos, selecciona la opción de mostrar artículos leídos y no leídos.

## Personalización opcional

En GitHub Actions puedes definir variables de entorno si quieres cambiar:

- `RECENT_KEEP` — recientes permanentes; por defecto 75.
- `BACKFILL_BATCH` — tamaño del lote histórico; por defecto 100.
- `BACKFILL_HOLD_HOURS` — horas que permanece cada lote; por defecto 24.
- `CHANNEL_HANDLE` — usuario del canal; por defecto `mariaspeaksenglish`.

## Notas técnicas

El workflow instala `yt-dlp[default]`, que incorpora el soporte EJS recomendado actualmente, y Deno 2.x como runtime JavaScript para YouTube. No descarga los vídeos: solo obtiene metadatos para construir el RSS.
