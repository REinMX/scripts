# IPM OpenServer User Guide (2025) — MBAL Chapter 5 reference

Source: working transcription/normalization of Chapter 5 text supplied by Javier on
2026-08-17. The supplied source identifies Petroleum Experts and carries
`© 1990-2025 PE Limited`.

This file preserves the parts needed to implement and audit the Python automation in
this repository. It is not a substitute for the licensed manual or the OpenServer help
in the installed MBAL version. Where the supplied extraction said only “etc.” or had
OCR-damaged tables, this reference does not invent missing variable names.

## 1. Rules that govern the implementation

- Every MBAL OpenServer variable starts with `MBAL`.
- Obtain a variable tag by focusing the exact GUI field and using
  **Ctrl + Right-click**. Use the displayed **Copy** action.
- Calculation buttons do not expose their command via Ctrl + Right-click. Use the
  documented `DoCmd` command list.
- A single OpenServer string transferred into MBAL is limited to **65,500
  characters**.
- Variables selected from a predefined list are written and read as their documented
  text keywords (for example `OIL`, `GAS`, `CON`, `WAT`, `YES`, `NO`), not as guessed
  integers.
- Tags and commands shown here must still be probed against the installed IPM/MBAL
  version before a licensed campaign.

## 2. Date handling

- Default MBAL input date format: `DD/MM/YYYY`.
- Input date unit: `MBAL.DATEUNITINPUT`.
- Prediction output date unit: `MBAL.DATEUNITOUTPUT`.
- Reference date: `MBAL.MB[0].REFDATE`.

Documented date-unit integer values:

| Value | Meaning |
|---:|---|
| 0 | Days |
| 1 | Weeks |
| 2 | Month |
| 3 | Year |
| 4 | Date (`DD/MM/YYYY`) |
| 5 | Date/time (`DD/MM/YYYY 00:00:00`) |
| 6 | Hour |
| 7 | Minute |
| 8 | Seconds |

For VBA/Excel, convert a retrieved `TIME` explicitly with `CDate()` when necessary to
avoid Excel flipping day and month.

## 3. General commands

| Command | Purpose |
|---|---|
| `MBAL.ChangeTool=MB` | Select material balance |
| `MBAL.ChangeTool=MC` | Select Monte Carlo |
| `MBAL.ChangeTool=DC` | Select decline curve |
| `MBAL.ChangeTool=1D` | Select 1D |
| `MBAL.ChangeTool=ML` | Select multi-layer |
| `MBAL.ChangeTool=TG` | Select tight gas |
| `MBAL.OpenFile` | Open an MBAL file |
| `MBAL.SaveFile(<path>)` | Save an MBAL file |
| `MBAL.NewFile` | Reset inputs / File → New |

Example command form:

`MBAL.OPENFILE("C:\path\model.mbi")`

## 4. Material-balance commands

| Command | Purpose |
|---|---|
| `MBAL.MB.RunPrediction` | Run a material-balance prediction |
| `MBAL.MB.RunSimulation` | Run a material-balance history simulation |
| `MBAL.MB.SavePred=<name>` or `MBAL.MB.SavePred("<name>")` | Copy current prediction stream |
| `MBAL.MB.SaveHist=<name>` or `MBAL.MB.SaveHist("<name>")` | Copy current history stream |
| `MBAL.MB.IMPORTTPD(<well>,"<file>")` | Import TPD into a prediction well |
| `MBAL.MB.IMPORTMBV(<well>,"<file>")` | Import MBV into a prediction well |
| `MBAL.MB.ALLOCTANKPRESSRATE(<tank>)` | Calculate tank history pressure and rate from wells |
| `MBAL.MB.ALLOCTANKRATEONLY(<tank>)` | Calculate tank history rate only |
| `MBAL.MB.RESETREGRESSTANKHIST(<tank>)` | Reset tank history-match regression inputs |
| `MBAL.MB.REGRESSTANKHIST(<tank>)` | Run tank history-match regression |
| `MBAL.MB.VALIDATE` | Validate objects created/populated through OpenServer |
| `MBAL.MB.LINKITEMS(<object1>,<object2>)` | Link MBAL objects |
| `MBAL.MB.BREAKLINK(<object1>,<object2>)` | Break a link |

The `=` form of `SavePred`/`SaveHist` cannot use `=` inside the stream name.

Other tool commands documented in the chapter:

- `MBAL.MC.Calculate`
- `MBAL.1D.RunSimulation`
- `MBAL.DC.RunPrediction`
- `MBAL.ML.Calculate`
- `MBAL.ML.Validate`
- `MBAL.PA.RUNALLOCATION`
- `MBAL.PA.SavePred=<name>` / `MBAL.PA.SavePred("<name>")`
- `MBAL.TG.RunPrediction`

## 5. Core material-balance variables used by this repository

### Tank identity and volume

The hierarchy is `MBAL.MB[0].TANK[...]`.

- Original oil in place: `.OOIP` (two letter O's; **not** `OIIP`).
- Original gas in place: `.OGIP`.
- Original water in place: `.OWIP`.
- Tank name: `.NAME`.
- Tank fluid type: `.TYPE`, one of `OIL`, `GAS`, `CON`, `WATER`.
- Initial pressure: `.PRESS`.
- Porosity: `.POROSITY`.

Name selection uses braces inside the index, for example:

`MBAL.MB[0].TANK[{Tank-1}].OOIP`

### Aquifer hierarchy

Aquifer data is under `TANK[...].AQUIF`, including:

- `.RD`
- `.ANGLE`
- `.PERM`
- `.VOLUME`
- `.DIFFUSIV`
- `.TD`
- `.MODEL`

The canonical aquifer-volume hierarchy used by this repository is therefore:

`MBAL.MB[0].TANK[...].AQUIF.VOLUME`

The supplied chapter does **not** document
`TANK[...].AQUIFER.VOLRATIO`. An aquifer multiplier remains possible only when an
exact version-specific tag is copied from the installed application.

## 6. Prediction setup and operational controls

Prediction setup is under `MBAL.MB[0].PREDINP`.

Important setup variables include:

- `CALCTYPE`
- `START`, `USERSTART`
- `END`, `USEREND`
- `STEPTYPE`, `USERSTEP`
- `WATINJ`, `GASINJ`, `GASLIFT`
- `GASREC`, `WATREC`
- `AQUPROD`, `GASCAP`
- `WATVOID`, `GASVOID`
- `RELPERM`, `CALCPOT`, `USEDCQ`
- reporting controls under `REPSTEPTYPE`, `REPSTEPVALUE`, `REPSTEPSTYLE`,
  `USERREPSTEP[i]`

Time-dependent production/injection controls are under:

`MBAL.MB[0].PREDINP.CONSTRAINT[i]`

Documented fields include:

- `TIME`
- `MANPRESS`
- `MINGASRATE`, `MAXGASRATE`
- `MAXWATRATE`, `MAXLIQRATE`
- `MININJGAS`, `MAXINJGAS`
- `GASINJMANPRESS`
- `MAX_GASLIFT`
- `WATINJMANPRESS`
- `MININJWATRATE`, `MAXINJWATRATE`
- recycling and voidage controls listed in the chapter

Consequences for this repository:

- A gas-lift **rate/availability ceiling** is written through
  `PREDINP.CONSTRAINT[i].MAX_GASLIFT`, not an undocumented
  `PREDWELL[well][i].GASLIFTRATE` default.
- A water-injection rate is written through
  `PREDINP.CONSTRAINT[i].MAXINJWATRATE`; fixed-rate mode can also set
  `MININJWATRATE` to the same value.
- These are prediction-constraint row indices. They are not tank-result rows.

## 7. Prediction wells

Prediction wells are under `MBAL.MB[0].PREDWELL[i]` or a name selector such as
`PREDWELL[{WellName}]`.

Documented fields include:

- `NAME`, `TYPE`, `DISABLED`
- `MINFBHP`, `MAXFBHP`
- `MINFWHP`, `MAXFWHP`
- `GASLIFTGLR`
- `OPTFREQ`
- `CONSTFBHP`
- `PERFORMTYPE`, one of `CFBHP`, `LIFTCURV`, `SMITH`, `WITLEY`
- `TPC.EXTRAPOLA`
- well abandonment constraints
- `IPR[i]` definitions and constraints

The manual does not list direct `PREDWELL[well][constraint].MAXRATE`, `.MINRATE`, or
`.GASLIFTRATE` fields in this hierarchy. Do not use those as defaults. A custom tag
may still be configured only when Ctrl + Right-click on the installed model returns
it.

## 8. Prediction results: `TRES`

Tank/leak results use:

`MBAL.MB[0].TRES[stream][sheet][row].<variable>`

Index semantics:

1. First index = stream:
   - `0`: production history
   - `1`: history simulation
   - `2`: production prediction
   - later indices: saved streams
2. Second index = result sheet:
   - single-tank case: sheet `0`
   - multi-tank case: sheet `0` is consolidated
   - next `N` sheets are the `N` tanks
   - following sheets are leaks
   - a verified sheet name can be used instead of its numeric index
3. Third index = time/result row.

The row count belongs to the selected stream/sheet, for example:

`MBAL.MB[0].TRES[2][3].COUNT`

The manual's VBA example reads count, then loops from row `0` while `i < COUNT`.
Therefore the last row is `COUNT - 1`.

The supplied chapter explicitly demonstrates result-row fields `TIME` and `OILRATE`.
The broader TRES catalog was abbreviated as “etc.” in the supplied extraction.
Common OpenServer examples use `OILRECOVER` and `TANKPRESS`, but they must be checked
with Ctrl + Right-click against the actual result table and units. Optional water
production/injection result tags are deliberately not assumed by the Python defaults.

## 9. Required simple prediction sequence

The Excel template names its helper procedures `DoCmd`, `DoSet`, and `DoGet`.
When Python dispatches `PX32.OpenServer.1` directly, the corresponding COM methods
are `DoCommand`, `SetValue`, and `GetValue`. Do not call nonexistent COM methods named
`DoSlowCommand`, `DoSet`, or `DoGet`.

The corresponding direct-Python sequence is:

1. Connect to OpenServer / MBAL.
2. `DoCommand("MBAL.OPENFILE(\"...\")")`.
3. `SetValue("MBAL.MB[0].TANK[{...}].OOIP", value)`.
4. `DoCommand("MBAL.MB.RunPrediction")`.
5. Read `TRES[2][sheet].COUNT` with `GetValue`.
6. Read the required row fields from row `COUNT - 1` with `GetValue` (or loop
   all rows).
7. Disconnect/shut down according to the general OpenServer lifecycle supported by
   the installed version.

## 10. Step-by-step material-balance prediction

Commands and internal variables:

- `MBAL.MB.STARTPRED`
- `MBAL.MB.NEXTSTEPPRED`
- `MBAL.MB.PREDFINISHED`
- `MBAL.MB.CURRENTPREDTIME`
- `MBAL.MB.CALCWELLS`
- `MBAL.MB.ENDPRED`

Pseudocode from the manual:

1. Start with `STARTPRED`.
2. Repeatedly call `NEXTSTEPPRED`.
3. After each step, read `PREDFINISHED` and `CURRENTPREDTIME`.
4. Optionally call `CALCWELLS` before `NEXTSTEPPRED` to evaluate well performance at
   current tank conditions.
5. Finish with `ENDPRED`.

Data that generally cannot be changed mid-prediction:

- all PVT inputs
- most tank data, except relative-permeability curves
- OOIP must not be changed mid-prediction

Data that can be changed for remaining prediction time:

- tank relative-permeability curves
- prediction production and constraints
- most prediction-well data
- well schedule

Do not delete schedule rows during a prediction. Stop their contribution by changing
end date or setting downtime to 100%.

## 11. Step-by-step production allocation

Commands and internal variables:

- `MBAL.PA.STARTALLOC`
- `MBAL.PA.NEXTSTEPALLOC`
- `MBAL.PA.ALLOCFINISHED`
- `MBAL.PA.CURRENTALLOCTIME`
- `MBAL.PA.ENDALLOC`

The same “changes affect only future steps” rule applies. PVT and most tank data cannot
be changed; relative permeability and most well data can be changed.

## 12. PVT commands

Material-balance examples (replace `MB` with the relevant tool where applicable):

- `MBAL.MB.PVT.INPUT.CALCULATE`
- `MBAL.MB.PVT.INPUT.MATCHCURRENT`
- `MBAL.MB.PVT.INPUT.MATCHALL`
- `MBAL.MB.PVT.INPUT.MATCHCURRENT(OIL)`
- `MBAL.MB.PVT.INPUT.MATCHCURRENT(CON)`
- `MBAL.MB.PVT.INPUT.MATCHALL(OIL)`
- `MBAL.MB.PVT.INPUT.MATCHALL(CON)`
- `MBAL.MB.PVT.INPUT.IMPORT("<file>")`
- `MBAL.MB.PVT.INPUT.IMPORT(OIL,"<file>")`
- `MBAL.MB.PVT.INPUT.IMPORT(CON,"<file>")`

For multiple PVT datasets, select by index or name, for example:

- `MBAL.MB.PVT.INPUT[1].CALCULATE`
- `MBAL.MB.PVT.INPUT[{LOWER}].CALCULATE`

Path-to-surface object commands are under `MBAL.MB[0].PVT.SETUP.PTS`, including
`ADD`, `REMOVE`, `IMPORT`, `EXPORT`, `RENAME`, equipment/connection add/remove, and
`UPDATEVALIDATION`.

## 13. Direct Access OpenServer (`MBAL.RL`)

Direct Access tags start with `MBAL.RL`. It operates on temporary calculation objects,
not the model currently displayed in the MBAL interface.

Command/data areas include:

- `GETDATA` / `SMBDATA`
- `RESCALC` / `SMBRESDATA`
- `WELLDATA` / `SMBWELLDATA`
- `IPRVLPCALC` / `SMBIPRVLPCALC`
- `RELPERMIPR`
- `RELPERMTANK`

Important lifecycle/high-level commands:

- `MBAL.RL.ALLRESET` at the start and end
- `MBAL.RL.INITIALISE("<mbi>")`
- `MBAL.RL.OPENFILE=<path>`
- `MBAL.RL.UPDATELAYERS`
- `MBAL.RL.CALCLAYERRATES`
- `MBAL.RL.CUMLAYERRATES`
- `MBAL.RL.DOSTEP`
- `MBAL.RL.SAVEFILE("<mbr>")`
- `MBAL.RL.RESET=<handle>`
- `MBAL.RL.DATETOTIME`
- `MBAL.RL.TIMETODATE`

Direct Access times are internal days since 1900. Use `DATETOTIME` and `TIMETODATE`
rather than hand-coding conversions.

The chapter's critical Direct Access sequencing rule is to call `UPDATELAYERS` every
prediction step; otherwise the calculations continue to use initial tank conditions.

Selected documented command IDs:

| Area | ID | Purpose |
|---|---:|---|
| `SMBRESDATA` / `RESCALC` | 8 | Get tank prediction data |
| `SMBRESDATA` / `RESCALC` | 9 | Set tank prediction data |
| `SMBWELLDATA` / `WELLDATA` | 4 | Get well-model count |
| `SMBWELLDATA` / `WELLDATA` | 10 | Get well-model name |
| `SMBWELLDATA` / `WELLDATA` | 12 | Set downtime |
| `SMBWELLDATA` / `WELLDATA` | 13 | Set number of wells |
| `SMBWELLDATA` / `WELLDATA` | 14 | Set start/end time |
| `SMBWELLDATA` / `WELLDATA` | 22 | Read well/IPR data |
| `SMBWELLDATA` / `WELLDATA` | 23 | Update PVT from tank |
| `SMBWELLDATA` / `WELLDATA` | 26 | Calculate rates from FBHP |
| `SMBWELLDATA` / `WELLDATA` | 27 | Calculate FBHP |
| `SMBWELLDATA` / `WELLDATA` | 35 | Add IPR/layer |
| `SMBWELLDATA` / `WELLDATA` | 36 | Correct rates for schedule |

Special Direct Access errors used in examples:

- `120`: no IPR/VLP solution
- `214`: well not found
- `216`: IPR/layer not found

## 14. Audit checklist for this repository

Before a licensed run:

- [ ] Open a working copy of the `.mbi`; preserve a separate backup.
- [ ] Run the target prediction manually.
- [ ] Confirm command is `MBAL.MB.RunPrediction` for the material-balance case.
- [ ] Confirm tank OOIP tags use `.OOIP`.
- [ ] Confirm name selectors include braces: `[{Object Name}]`.
- [ ] If varying aquifer volume, confirm `.AQUIF.VOLUME` in this MBAL version.
- [ ] Confirm the prediction constraint row for `MAX_GASLIFT` and/or
      `MININJWATRATE`/`MAXINJWATRATE`.
- [ ] Confirm `TRES` stream and sheet for every tank.
- [ ] Confirm `COUNT > 0`, last row = `COUNT - 1`, terminal date, cumulative oil,
      pressure, and units against the GUI.
- [ ] Copy optional water-result tags instead of inferring their names.
- [ ] Start with one realization and compare it manually before the full campaign.
