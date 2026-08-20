# Runbook de MBAL/OpenServer: del modelo corregido a la campaña

> **Primer paso en el trabajo:** abre **Petroleum Experts MBAL** y carga el
> `.mbi` ya corregido. No empieces con Python. Primero confirma el modelo,
> crea la copia de seguridad y recoge los identificadores/tags de esa versión
> instalada.

Este procedimiento es para ejecutar `mbal.py` en el mismo equipo Windows que
tiene MBAL y OpenServer licenciados.


El repositorio es público. Los nombres reales, rutas, tags copiados del modelo,
volúmenes, resultados y archivos de trabajo deben vivir solamente en archivos
locales ignorados por Git.

## 1. Abrir, comprobar y proteger el modelo

1. Abre MBAL.
2. Carga el `.mbi` corregido.
3. Confirma visualmente que es la revisión correcta: fecha/revisión interna,
   tanques, pozos, PVT, acuíferos, conexiones, historial y predicción.
4. En MBAL, ejecuta **la predicción que se automatizará** una vez de forma
   manual, sin cambiar sus datos. Debe terminar sin error ni diálogo pendiente.
5. Anota para esa ejecución manual:
   - nombre o índice visible de la predicción;
   - fecha final;
   - petróleo acumulado final por tanque y total;
   - presión final por tanque;
   - agua acumulada final, si se va a reportar;
   - unidades mostradas;
   - ajuste operativo usado, incluido gas lift si aplica.
6. Usa **Save As / Guardar como** para crear una copia separada, por ejemplo
   `modelo_pre_openserver_backup.mbi`. No automatices sobre la única copia.
7. Crea además una copia de trabajo, por ejemplo
   `modelo_openserver_working.mbi`. Esta será la ruta de `mbal_file`.
8. Mantén el backup sin tocar. Si MBAL pregunta si debe guardar cambios al
   cerrar, no sobrescribas el backup.

**Antes de cualquier ejecución con licencia, abre MBAL y déjalo abierto.**
OpenServer se conecta a un MBAL que ya está corriendo; no lo arranca. Abre
MBAL una vez, cierra cualquier diálogo inicial y no lo cierres entre corridas.

El script envía `MBAL.OPENFILE`, cambia valores en la sesión, ejecuta el comando
de predicción y lee resultados. Al terminar **deja MBAL abierto** para la
siguiente corrida; `close_mbal_on_finish: true` envía el comando de cierre
configurado. **No envía un comando de guardado**, pero eso no sustituye la
copia de seguridad: MBAL, la instalación o un diálogo pueden tener
comportamiento adicional.

## 2. Qué permanece en MBAL y qué cambia Python

| Permanece definido en el `.mbi` | Python cambia o ejecuta por realización |
|---|---|
| PVT, contactos, transmisibilidades y acuíferos base | STOIIP de cada tanque configurado |
| Historia y estado inicial del modelo | Multiplicador o volumen de acuífero, solamente si se configura |
| Pozos, conexiones, IPR/VLP y equipo | Tasa de gas lift, solamente durante un sweep configurado |
| Fechas, pasos, constraints y lógica de predicción | Controles de inyección, solamente si se configuran |
| Definición de la predicción | Comando OpenServer para calcular esa predicción |
| Unidades y variables disponibles en la versión instalada | Lectura del último paso: Np, presión y resultados opcionales |

Python **no** crea tanques, pozos, conexiones, una predicción nueva ni corrige
el modelo. Tampoco puede determinar por sí solo el tag correcto de una versión
de IPM: hay que copiarlo y verificarlo en MBAL.

## 3. Preparar Windows, Python y pywin32

Abre PowerShell en la raíz del repositorio:

```powershell
py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pywin32
```

Verifica que se usa el intérprete esperado y que pywin32 carga:

```powershell
python -c "import sys, struct; print(sys.executable); print('Python bits:', struct.calcsize('P')*8)"
python -c "import win32com.client; print('pywin32 OK')"
python mbal.py --help
```

También deben cumplirse estos prerrequisitos:

- Windows y MBAL/OpenServer instalados en el mismo equipo;
- licencia válida para abrir y calcular el modelo;
- arquitectura de Python compatible con el COM registrado;
- permisos de lectura sobre la copia `.mbi` y de escritura sobre el directorio
  local de resultados;
- ninguna sesión GUI editando la misma copia de trabajo durante la campaña.

El **ProgID no se adivina**. Recógelo de la documentación OpenServer incluida
con la instalación, de un ejemplo oficial que funcione en ese equipo o de la
configuración COM registrada que mantenga IT/Petroleum Experts. Luego se prueba
con `--check-openserver` en la sección 8.

## 4. Recoger nombres y strings OpenServer sin adivinarlos

Los menús y strings cambian entre versiones. El resultado de este paso debe ser
una lista de strings literales copiados de **la instalación y el modelo reales**.
No conviertas todavía un string en plantilla.

### 4.1 Nombres e índices de objetos

1. Con la copia corregida abierta, ve al árbol/listado de tanques.
2. Copia el nombre exacto de cada tanque: mayúsculas, espacios, guiones y
   puntuación. Anota también el índice cero-based que usa OpenServer si la
   versión trabaja por índice. No deduzcas el índice del orden visual sin
   verificarlo en el browser.
3. Abre la predicción correcta y copia su nombre/identificador y su índice.
4. En la tabla de pozos de esa predicción, copia el nombre exacto del productor
   con gas lift y el índice del pozo si el modo de tags es por índice.
5. Anota el índice del bloque/paso de predicción que contiene el control. Un
   valor frecuente en otro modelo no es evidencia para este modelo.

### 4.2 Inputs por tanque

Para cada tanque configurado:

1. Navega al campo de STOIIP/OOIP que realmente controla el tanque.
2. Da foco al campo.
3. Usa el browser de variables OpenServer o la acción de copiar access string
   de la versión instalada. En algunas versiones es `Ctrl` + clic derecho;
   confirma el gesto exacto en la ayuda de esa instalación.
4. Pega el string **literal** en la hoja de recogida de la sección 5.
5. Lee el valor con el browser y comprueba que coincide con la GUI y su unidad.
6. Si se variará acuífero, repite para el multiplicador o volumen exacto. No
   confundas volumen de acuífero con ratio/multiplicador.

Copia al menos dos tanques. Solamente después compara los strings para decidir
si el template usa `{tank}` o `{i}`. Conserva todo lo demás exactamente como lo
entrega MBAL.

### 4.3 Gas lift

1. Abre el control de gas lift dentro de **esa predicción**. En la jerarquía
   soportada, el límite de tasa está en
   `PREDINP.CONSTRAINT[i].MAX_GASLIFT`, no en el tag antiguo/no documentado
   `PREDWELL[well][i].GASLIFTRATE`.
2. Confirma que al modificarlo manualmente cambia el caso que se pretende
   estudiar.
3. Copia el access string literal.
4. Anota la unidad de entrada mostrada por MBAL/OpenServer. El script envía el
   número tal cual; no convierte la tasa.
5. Comprueba el índice de la fila `CONSTRAINT`. El tag por defecto es de campo y
   no usa nombre de pozo; un tag por pozo solamente es válido si fue copiado de
   la instalación.

### 4.4 Comando y resultados

En el browser/command browser de OpenServer de la versión instalada:

1. Identifica y copia el comando que calcula la predicción ya probada a mano.
   Para material balance, usa `MBAL.MB.RunPrediction`.
2. Identifica cómo obtener el número de pasos de resultados. Verifica si
   `COUNT` representa cantidad y si el último índice es `COUNT - 1`.
3. Para **cada tanque**, copia el string de petróleo acumulado final.
4. Repite para presión final y agua acumulada final si se reportará.
5. Anota dónde aparece el índice de tanque, el índice temporal y el token de
   unidad.
6. Con los resultados de la ejecución manual aún disponibles, lee cada tag y
   compara número y unidad. No aceptes solamente “el tag no da error”.
7. Solamente cuando dos tanques/pasos confirmen el patrón, sustituye en la copia
   local las partes variables por `{i}`/`{r}`, `{tank}`, `{k}` y `{u}`.

Los resultados de predicción son `TRES[2][sheet][row]`. En un modelo
multi-tanque, `sheet=0` es consolidado y las siguientes hojas son los tanques.
No reutilices automáticamente el índice del objeto tanque como hoja TRES:
configura `tanks[].result_index` cuando uses tags numéricos.

`--check-openserver` verifica COM, apertura y lectura de **inputs**. El comando
de predicción y los result tags se verifican en la corrida mínima de una
realización; Linux no puede verificarlos.

## 5. Hoja imprimible de recogida manual

Marca cada fila como `copiado`, `leído` y `comparado con GUI`. No rellenes una
celda por analogía con otra versión.

| Dato obligatorio | Valor privado local | Unidad / evidencia | Verificado |
|---|---|---|---|
| Ruta absoluta de la **copia de trabajo** `.mbi` | ____________________ | archivo abre en MBAL | ☐ |
| Ruta del backup `.mbi` | ____________________ | no se automatiza | ☐ |
| OpenServer ProgID | ____________________ | fuente de la instalación | ☐ |
| Nombre/índice de la predicción | ____________________ | predicción manual correcta | ☐ |
| Comando para ejecutar predicción | ____________________ | command browser | ☐ |
| Fecha/paso terminal esperado | ____________________ | resultados manuales | ☐ |
| Tag de cantidad de pasos (`res_nsteps`) | ____________________ | COUNT e índice final comprobados | ☐ |
| Unidad STOIIP (`unit_stoiip`) | ____________________ | GUI = OpenServer | ☐ |
| Unidad presión (`unit_press`) | ____________________ | GUI = OpenServer | ☐ |
| Unidad acumulados (`unit_cum`) | ____________________ | GUI = OpenServer | ☐ |
| `tag_mode`: nombre o índice | ____________________ | dos tanques comparados | ☐ |
| Tanque 1: `key`, nombre exacto, índice | ____________________ | árbol + browser | ☐ |
| Tanque 1: `result_index`/hoja TRES | ____________________ | `TRES[2]`, 0 es consolidado | ☐ |
| Tanque 1: tag STOIIP literal | ____________________ | lectura coincide | ☐ |
| Tanque 1: tag acuífero y tipo, si aplica | ____________________ | volumen o multiplicador | ☐ |
| Tanque 1: tag Np + unidad | ____________________ | terminal manual | ☐ |
| Tanque 1: tag presión + unidad | ____________________ | terminal manual | ☐ |
| Tanque 1: tag Wp + unidad, si aplica | ____________________ | terminal manual | ☐ |
| Tanque 2: `key`, nombre exacto, índice | ____________________ | árbol + browser | ☐ |
| Tanque 2: `result_index`/hoja TRES | ____________________ | `TRES[2]`, 0 es consolidado | ☐ |
| Tanque 2: tag STOIIP literal | ____________________ | lectura coincide | ☐ |
| Tanque 2: tag acuífero y tipo, si aplica | ____________________ | volumen o multiplicador | ☐ |
| Tanque 2: tag Np + unidad | ____________________ | terminal manual | ☐ |
| Tanque 2: tag presión + unidad | ____________________ | terminal manual | ☐ |
| Tanque 2: tag Wp + unidad, si aplica | ____________________ | terminal manual | ☐ |
| Cada tanque adicional: mismo juego | ____________________ | una fila por tag | ☐ |
| Pozo de gas lift: nombre e índice | ____________________ | prediction well browser | ☐ |
| Índice del bloque/paso de gas lift | ____________________ | string copiado | ☐ |
| Tag de gas lift literal | ____________________ | lectura coincide | ☐ |
| Unidad de gas lift | ____________________ | **sin conversión Python** | ☐ |
| Grid privado de gas lift | ____________________ | unidad anterior | ☐ |
| Np total manual de referencia | ____________________ | misma fecha/unidad | ☐ |
| Presiones manuales de referencia | ____________________ | misma fecha/unidad | ☐ |

Añade filas equivalentes para todo tanque y todo resultado que realmente se
usará. En el código actual, `res_nsteps`, `res_cumoil` y `res_pressure` son
obligatorios en la configuración; `res_nsteps` debe verificarse para evitar
leer por error el primer paso. `res_cumwat` es opcional en código pero
obligatorio si el estudio reportará agua.

## 6. Crear la configuración privada local

Cierra MBAL después de recoger y guardar los tags. La automatización abrirá la
copia de trabajo por COM.

En PowerShell, desde el repositorio:

```powershell
Copy-Item .\example.yaml .\mbal_config.local.yaml
Copy-Item .\mbal_config.local.yaml .\mbal_smoke.local.yaml

git check-ignore -v -- .\mbal_config.local.yaml .\mbal_smoke.local.yaml
git status --short --ignored
```

Ambos YAML deben aparecer como ignorados (`!!` en la vista de ignorados). Edita
**solamente** esos archivos locales:

- `mbal_file` y `openserver_prog_id`;
- `tag_mode`;
- `tanks[].name`, `tanks[].index`, `tanks[].result_index` y priors;
- índice de `PREDINP.CONSTRAINT`, unidad y valores de gas lift;
- `gas_lift_well` solamente si un tag por pozo fue verificado y configurado;
- acuíferos opcionales;
- `tags` literales/templates ya verificados;
- unidades y directorios de salida bajo `work\...`.

Para la primera prueba, deja vacíos los sweeps que no se van a verificar. En
`mbal_smoke.local.yaml`, usa una sola condición operativa verificada.

Validación estática, sin crear outputs y sin abrir MBAL:

```powershell
python mbal.py --config .\mbal_config.local.yaml --validate-config
```

Esta validación detecta estructura, distribuciones, tags obligatorios y textos
`REPLACE_WITH_`; **no** demuestra que el ProgID o los strings correspondan a la
versión instalada.

### Probar que no hay datos privados en el diff público

```powershell
git check-ignore -v -- .\mbal_config.local.yaml .\mbal_smoke.local.yaml
git ls-files --error-unmatch .\mbal_config.local.yaml
git diff --check
git diff -- .
git diff --cached -- .
```

`git ls-files --error-unmatch` debe terminar con error porque el YAML local no
está trackeado. `git diff` y `git diff --cached` no deben contener rutas,
nombres, tags privados, volúmenes del activo ni resultados.

Busca además cada token privado antes de cualquier commit:

```powershell
$privateTokens = @("PEGAR_NOMBRE_PRIVADO_1", "PEGAR_NOMBRE_PRIVADO_2")
foreach ($token in $privateTokens) {
    git grep -nF -- $token
    if ($LASTEXITCODE -eq 0) { throw "Dato privado trackeado: $token" }
}
```

Sustituye los textos de la lista localmente. No guardes esa lista en un archivo
trackeado.

## 7. Configuración científica: tanques independientes

Cada tanque requiere `official_stoiip`. Sin P90/P10, ese volumen es fijo. Con
ambos límites, `official_stoiip` es el P50 y debe cumplirse
`0 < P90 < official < P10`.

La implementación actual asigna una dimensión MC/LHS propia a cada tanque y
deriva `stoiip_total` fila por fila como suma. No tiene conectividad, grupos,
correlación, `field_scale`, multiplicadores residuales ni ajustes del total de
campo. Si el estudio necesita una dependencia física, debe añadirse como cambio
explícito del modelo; no la escondas en los tres marginales.

Dry run recomendado para revisar marginales y rangos antes de OpenServer:

```powershell
python mbal.py --config .\mbal_config.local.yaml --dry-run --n 1000 --out-dir .\work\independent_dry
```

Para independencia intencionada y sin factores compartidos, la matriz de
correlación de rangos debe acercarse a cero con una muestra suficiente. No uses
un umbral rígido con muestras pequeñas. El P90/P50/P10 del total de campo es un
resultado; no sumes percentiles de tanque.

## 8. Secuencia de pruebas antes de la campaña completa

### 8.1 Smoke test de COM, modelo e inputs (no escribe)

Con MBAL GUI cerrado y la copia de trabajo disponible:

```powershell
python mbal.py --config .\mbal_smoke.local.yaml --check-openserver
```

Debe confirmar:

- dispatch del ProgID;
- apertura de la copia `.mbi`;
- lectura de cada input configurado;
- cierre de la sesión sin escribir inputs ni calcular predicción.

Si falla, no uses `--no-validate-tags` para ocultarlo. Corrige ProgID, ruta,
objeto o tag.

### 8.2 Dry run completo (no abre MBAL)

```powershell
python mbal.py --config .\mbal_smoke.local.yaml --dry-run --n 20 --out-dir .\work\smoke_dry
```

Comprueba `samples_dry_run.csv`, rangos, unidades declaradas, suma por fila,
correlación y número de filas.

### 8.3 Corrida licenciada mínima

En `mbal_smoke.local.yaml`, deja una sola condición de gas lift y ningún otro
sweep. Luego:

```powershell
python mbal.py --config .\mbal_smoke.local.yaml --n 1 --stop-on-error --out-dir .\work\smoke_licensed
```

Esta es la primera operación que escribe inputs y calcula. Revisa:

1. exactamente una fila con `status == ok`;
2. lectura correcta de `TRES[2][sheet].COUNT`; si falla, el programa debe
   detener la realización y nunca caer a la fila 0;
3. Np, presión y Wp en unidades correctas;
4. valores no nulos y sensibles a los inputs;
5. log sin diálogos ni errores OpenServer.

Después abre de nuevo la copia de trabajo en MBAL, introduce manualmente los
inputs guardados en la fila de smoke, ejecuta la misma predicción y compara los
resultados terminales. Las diferencias deben explicarse por redondeo, no por
otro tanque, índice, fecha o unidad.

## 9. Campañas recomendadas

### 9.1 Incertidumbre de volumen/tanques

Con la configuración independiente o correlacionada ya justificada:

```powershell
python mbal.py --config .\mbal_config.local.yaml --dry-run --n 1000 --out-dir .\work\volume_dry
python mbal.py --config .\mbal_config.local.yaml --n 500 --out-dir .\work\volume_full
```

Usa `--n` y el horizonte aprobados para el estudio; los valores anteriores son
una secuencia operativa, no un tamaño estadístico universal.

### 9.2 Sweep pareado de gas lift

Pon el grid privado y su unidad verificada en `mbal_config.local.yaml`, y deja
sin `values:` los controles que no quieras barrer, salvo que busques
explícitamente el producto cartesiano.

```powershell
python mbal.py --config .\mbal_config.local.yaml --dry-run --n 200 --out-dir .\work\gas_lift_dry
python mbal.py --config .\mbal_config.local.yaml --n 200 --out-dir .\work\gas_lift_full
```

Cada
`base_realization` se repite para todas las tasas: la comparación de lift es
pareada y no confunde diferencias geológicas con diferencias operativas.

**No agrupes resultados de producción de todas las tasas en un único
percentil.** `summary_percentiles.csv` resume la geología una vez por
realización base. Para petróleo por tasa, el resultado canónico es
`gas_lift_sensitivity.csv` (nombrado por el control, con su PNG), con P90/P50/P10 absolutos y del delta
pareado respecto a la tasa mínima. Si también hay inyección, cada combinación
permanece separada; no se agrupan controles diferentes.

## 10. Resume, archivos y reinicio seguro

El CSV principal se escribe una fila a la vez con un esquema estable.

- En un restart, solamente `status == ok` se omite.
- Las filas fallidas se reintentan.
- Los inputs almacenados se comparan con la tabla determinista regenerada.
- Si cambian seed, priors, orden/grid de controles o estructura de muestras, el
  resume se rechaza para no mezclar campañas.

Reinicia con **el mismo** YAML, seed, `out_dir`, `out_csv` y CLI:

```powershell
python mbal.py --config .\mbal_config.local.yaml --n 500 --out-dir .\work\volume_full
```

Para regenerar tablas/gráficos sin MBAL:

```powershell
python mbal.py --config .\mbal_config.local.yaml --summarize-only --out-dir .\work\volume_full
```

Archivos principales:

| Archivo | Uso |
|---|---|
| `mbal_results.csv` o `out_csv` local | inputs, resultados, status y runtime por fila |
| `summary_percentiles.csv` | oficial y P90/P50/P10 de tanque/campo; no pool operativo |
| `<control>_sensitivity.csv` | Np absoluto e incremental pareado por cada control barrido |
| `run_metadata.csv` | seed, muestreo y conteos |
| `mbal_run.log` | errores, progreso y ETA |
| `*.png` | histogramas, excedencia y sensibilidades |

### Parada segura

1. Espera, si es posible, a que aparezca el log de fin de la realización
   actual; la fila ya estará escrita.
2. Pulsa `Ctrl+C` una sola vez en la consola.
3. Espera a que MBAL termine el comando bloqueante y cierre la sesión.
4. Si hay un diálogo MBAL, resuélvelo en la GUI; un `DoCommand` bloqueado
   dentro de COM no tiene timeout real en este proceso.
5. Usa Task Manager solamente como último recurso. Después abre la copia de
   trabajo manualmente y comprueba integridad antes de reiniciar.
6. Reanuda con exactamente el mismo comando. No edites el CSV a mano.

## 11. Comprobaciones de calidad

Define el resultado principal y ejecuta esta revisión básica:

```powershell
$env:MBAL_RESULTS = ".\work\gas_lift_full\mbal_results.csv"
python -c "import os,pandas as pd; d=pd.read_csv(os.environ['MBAL_RESULTS']); print('rows',len(d)); print(d['status'].value_counts(dropna=False)); assert d['realization'].nunique()==len(d)"
```

### Conteos

- Sin sweep: filas esperadas = `n_realizations`.
- Solo gas lift: filas = realizaciones base × número de tasas.
- Varios controles: producto cartesiano de todas las listas por realizaciones
  base.
- Cada `base_realization` debe contener exactamente el grid operativo completo.
- El número de filas `ok` debe ser el aceptado por el estudio; investiga toda
  fila `failed:` y no la ocultes al resumir.
- Las tablas de sensibilidad muestran `n_expected`, `n_ok`, `n_failed`,
  `n_missing` y `success_fraction` por combinación. No compares percentiles si
  la cobertura es incompleta o desigual.
- `delta_P90/P50/P10` y `probability_delta_positive` usan solamente pares con el
  mismo `base_realization`; `n_paired` debe ser el esperado.

### Orden de percentiles

```powershell
$env:MBAL_SUMMARY = ".\work\gas_lift_full\gas_lift_sensitivity.csv"
python -c "import os,pandas as pd; s=pd.read_csv(os.environ['MBAL_SUMMARY']).dropna(subset=['P90','P50','P10']); assert ((s.P90<=s.P50)&(s.P50<=s.P10)).all(); print(s.to_string(index=False))"
```

La convención O&G usada es P90 = percentil 10 (caso bajo) y P10 = percentil 90
(caso alto).

### Independencia y suma

En el dry run:

- comprueba por fila `stoiip_total == sum(stoiip_<tanque>)`;
- revisa correlación de rangos aproximadamente nula con una muestra suficiente;
- confirma que no aparecen columnas de conectividad, escala o residual;
- no sumes P90/P50/P10 marginales para construir percentiles de campo.

### Consistencia MBAL

- `0 <= Np_tanque <= STOIIP_tanque` bajo las unidades/definiciones del caso;
- RF de campo se calcula por fila como `sum(Np_i)/sum(STOIIP_i modelado)`, no
  como promedio de RFs;
- presiones dentro de límites físicos y consistentes con la fecha final;
- acumulados monotónicos respecto al tiempo en el resultado manual;
- una variación material de STOIIP o lift debe producir la respuesta esperada,
  salvo que el caso esté limitado por tasa/constraint;
- el caso de una realización debe reproducirse manualmente con sus mismos
  inputs y condición operativa.

## 12. Troubleshooting

### COM o ProgID falla

- confirma que estás en Windows y en el Python/venv correcto;
- confirma `import win32com.client`;
- confirma licencia e instalación local de MBAL/OpenServer;
- copia el ProgID de una fuente válida de **esa instalación**; no pruebes una
  lista de nombres inventados;
- revisa compatibilidad 32/64 bit con el COM registrado;
- abre MBAL manualmente una vez y elimina cualquier diálogo inicial;
- repite `--check-openserver`.

### Tag inválido

- vuelve al campo exacto y copia de nuevo el access string literal;
- verifica `tag_mode`, nombre/índice, mayúsculas, espacios y comillas;
- compara dos tanques antes de introducir `{tank}` o `{i}`;
- verifica `{p}`, `{k}` y `{u}` en el browser;
- no uses `--no-validate-tags` para avanzar a la campaña.

### Tanque o índice de predicción equivocado

- comprueba el índice cero-based en OpenServer, no solamente la posición GUI;
- usa la copia de trabajo y una corrida mínima con inputs claramente distintos
  entre tanques;
- verifica que el tanque que cambia en MBAL coincide con la columna escrita;
- confirma que el control de gas lift pertenece al bloque/paso de predicción
  que se calcula.

### CSV stale o resume rechazado

- no edites el CSV;
- si cambió cualquier input, seed, prior o grid, usa un `out_dir` nuevo;
- si no cambió, compara el YAML y el comando con la ejecución original;
- conserva el directorio rechazado como evidencia, no mezcles filas.

### MBAL muestra diálogos o queda bloqueado

- detén la campaña y resuelve manualmente warnings de licencia, guardado,
  convergencia o archivos;
- consigue que la predicción termine a mano sin intervención;
- vuelve a la corrida `--n 1`;
- un timeout alrededor de `DoCommand` en el mismo proceso no interrumpe de
  forma segura un COM bloqueado.

### Unidades incorrectas

- compara GUI, access string, `unit_stoiip`, `unit_press`, `unit_cum` y unidad
  del grid;
- recuerda que gas lift se escribe como número crudo en la unidad del modelo;
- no cambies solamente la etiqueta YAML esperando una conversión;
- repite la reproducción manual de una fila.

### Resultados cero, idénticos o sin respuesta

- verifica que se calculó la predicción correcta;
- verifica el comando de cálculo copiado;
- el código ya no acepta fallback a result index 0: corrige `res_nsteps`/`COUNT`;
- confirma stream `2`, hoja `{r}`/`{tank}`, `{k}=COUNT-1` y fecha terminal;
- revisa si un constraint de tasa hace al caso rate-limited;
- verifica que se cambió el input del tanque/pozo correcto y que no fue
  sobreescrito por otra regla de predicción.

## 13. Checklist de fin de corrida

- [ ] Backup `.mbi` intacto y copia de trabajo separada.
- [ ] Config local y `.mbi` aparecen ignorados por Git.
- [ ] Config estática, smoke OpenServer, dry run y `--n 1` pasaron.
- [ ] Comando y result tags se reprodujeron contra una ejecución interactiva en MBAL.
- [ ] Conteo total, grid pareado y `base_realization` son correctos.
- [ ] Fallos investigados; no hay filas fallidas aceptadas silenciosamente.
- [ ] P90 ≤ P50 ≤ P10.
- [ ] Unidades, fecha final, Np, presión, Wp y RF son consistentes.
- [ ] Las tablas de sensibilidad tienen cobertura completa, `n_paired`
      esperado y no mezclan combinaciones de controles.
- [ ] Resume fue hecho solamente con inputs idénticos.
- [ ] `git diff`, `git diff --cached` y búsqueda de tokens no muestran datos
      privados.
- [ ] No se añadieron al commit nombres de activo, tags locales, `.mbi`, CSV,
      imágenes, logs ni datos de trabajo.

## 14. Advertencia pública

**NO COMMIT:** identificadores de activo, nombres de campo/modelo/tanque/pozo,
rutas de red o locales, strings OpenServer específicos, archivos `.mbi`,
backups, licencias, priors del activo, históricos, grids operativos privados,
CSV, figuras, logs ni resultados de trabajo.

Los templates trackeados deben permanecer anónimos. Si un dato real entra en un
archivo trackeado, retíralo antes del commit y revisa también el historial si ya
fue confirmado.