# Zotero para Claude Code

Conecta tu biblioteca de Zotero con Claude Code. Claude puede navegar tus carpetas, buscar y leer
tus artículos (incluido el texto de los PDF), y ayudarte a organizar la biblioteca: etiquetas,
carpetas, corregir metadatos, unir duplicados y escribir notas.

**Todo cambio se puede deshacer.** Cada cambio queda registrado con un identificador; basta con
pedirle a Claude "deshace el último cambio". Nada se borra definitivamente: lo que se elimina va
a la papelera de Zotero.

## Qué necesitas

- **Zotero 7** (o posterior) abierto mientras usas Claude.
- **Sincronización de Zotero activada** (Editar → Configuración → Sincronización). Los cambios
  se guardan en zotero.org y llegan a tu Zotero al sincronizar, normalmente en segundos.
- **Claude Code** instalado.

## Instalación en Windows (una sola vez)

1. **Permitir que Claude lea Zotero.** En Zotero: Editar → Configuración → Avanzado → marcar
   *"Permitir que otras aplicaciones de este equipo se comuniquen con Zotero"*.

2. **Crear una clave de API de Zotero** (para que Claude pueda organizar; sin ella solo lee).
   Entra en <https://www.zotero.org/settings/keys/new> y marca:
   - Personal Library → *Allow library access*, *Allow notes access* y *Allow write access*.
   - Default Group Permissions → *None*.

   Guarda la clave. Es como una contraseña: no la compartas.

3. **Instalar `uv`** (ejecuta los programas en Python). En PowerShell:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   Cierra y vuelve a abrir PowerShell.

4. **Copiar esta carpeta** a tu equipo, por ejemplo a `C:\Users\TU_USUARIO\zotero-mcp`.

5. **Registrar el conector en Claude Code** (cambia la ruta y la clave):
   ```powershell
   claude mcp add zotero --scope user -e ZOTERO_API_KEY=TU_CLAVE -- uv --directory C:\Users\TU_USUARIO\zotero-mcp run zotero-mcp
   ```

6. **Comprobar.** Abre Claude Code y escribe: *"comprueba la conexión con Zotero"*. Debería
   responder con el número de artículos y carpetas de tu biblioteca.

## Ejemplos de uso

- "Muéstrame mis carpetas de Zotero"
- "¿Qué artículos tengo en Tesis/Capítulo 2?"
- "Busca en mis PDFs qué artículos hablan de aprendizaje por refuerzo"
- "Lee el artículo de García 2021 y resúmelo; guarda el resumen como nota en Zotero"
- "Revisa la carpeta Lecturas y propón etiquetas por tema"
- "¿Qué artículos no están en ninguna carpeta? Propón dónde archivarlos"
- "Busca duplicados y únelos"
- "Revisa los metadatos de mis artículos sin DOI y corrígelos con Crossref"
- "Muéstrame el historial de cambios" / "Deshaz el cambio 1110-153012-ab12"

Para cambios que afectan a más de 5 artículos, Claude primero muestra una **vista previa** y
espera tu confirmación. Además, nunca cambia más de 100 artículos de una vez.

## Detalles

- Las lecturas van directo a Zotero en tu equipo. Los cambios van a zotero.org con tu clave.
  Para corregir metadatos se consulta Crossref (api.crossref.org) sin enviar datos personales.
- El registro de cambios está en `%LOCALAPPDATA%\zotero-mcp\changes.jsonl`.
- Las anotaciones o subrayados de los PDF no se leen; sí el texto completo y las notas.
- Un PDF escaneado sin OCR no tiene texto: Claude te lo dirá.

### Configuración opcional (variables de entorno)

| Variable | Por defecto | Uso |
|---|---|---|
| `ZOTERO_API_KEY` | (vacía, solo lectura) | Clave de zotero.org |
| `ZOTERO_MCP_MAX_BATCH` | `100` | Máximo de artículos por cambio |
| `ZOTERO_MCP_TIMEOUT` | `20` | Segundos de espera a Zotero |
| `ZOTERO_MCP_DATA_DIR` | `%LOCALAPPDATA%\zotero-mcp` | Dónde se guarda el registro de cambios |

## Desarrollo

```sh
uv run pytest        # pruebas contra un Zotero simulado (no toca ninguna biblioteca real)
```
