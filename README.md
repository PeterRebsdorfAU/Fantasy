# ⚽ FPL Draft Liga Dashboard

Henter data fra jeres **FPL Draft**-liga (draft.premierleague.com) og viser det i browseren.

## Kom i gang
1. Dobbeltklik på **`Start FPL Liga.command`** (eller kør `python3 fpl_liga.py` i Terminal).
2. Browseren åbner. Indsæt **linket fra draft-siden**:
   - Gå til draft.premierleague.com → klik på **Points** (eller på et hold i ligatabellen)
   - Kopier hele adressen. Den ser sådan ud: `https://draft.premierleague.com/entry/123456/event/5`
   - Programmet finder selv ligaen ud fra holdet. Du kan også skrive ligaens ID direkte.
3. Programmet husker ligaen til næste gang. Luk Terminal-vinduet for at stoppe det.

## Det kan dashboardet vise
- **Stilling** med op/ned-pile, V-U-T, point for/imod og form (de seneste 5)
- **Næste runde**: kampene med begge holds form og tidligere opgør
- **Win/loss streaks**: nuværende, længste sejrsstime og nederlagsstime, ubesejret og uden sejr
- **Udvikling**: graf over placering, ligapoint og point pr. runde
- **Pointtavle**: heatmap over alle runder med 👑 for rundens vinder og 🥄 for sidstepladsen
- **Priser**: Rundens konge, Træskeen, Rekordrunden, Ligaens MVP, Draft-røveriet, Draft-fiaskoen,
  Årets waiver-fund, Den der slap væk, Bænkekongen, Heldig kartoffel, Røveren m.fl.
- **Alle mod alle**: hvem ville vinde, hvis alle spillede mod alle hver runde, og hvem har haft held
- **Draft & waivers**: draftens røverier og fiaskoer, hvert holds MVP, bedste waiver-fund,
  spillere der blev smidt ud og siden scorede, og de bedste ledige spillere lige nu
- **Bænk & transaktioner**: waivers, free agents, bænkpoint
- Klik på en manager for at se hele sæsonen runde for runde, inkl. draft-valg

## Ekstra
```bash
python3 fpl_liga.py "https://draft.premierleague.com/entry/123456/event/5"   # start direkte
python3 fpl_liga.py --export rapport.html     # gem en statisk rapport, som kan deles i ligaen
```

Data fra afsluttede runder gemmes i mappen `cache/`, så det går hurtigere næste gang.
Mappen kan sagtens slettes. Programmet kræver kun Python 3 og ingen ekstra pakker.

## Online på Render (static site)

FPL Draft tillader ikke, at en hjemmeside henter data direkte fra browseren. Derfor hentes
data, når Render **bygger** siden, og lægges ind i `index.html`. Der skal ikke bruges nogen database.
En GitHub Action beder Render om at bygge siden igen hver 4. time, så tallene holdes friske.

### 1. GitHub
Upload alle filerne i mappen til repoet, også de skjulte `.gitignore` og `.github/workflows/opdater-data.yml`.
Tryk **Cmd + Shift + .** i Finder for at vise skjulte filer.

### 2. Render: New → Static Site → vælg repoet
| Felt | Værdi |
|---|---|
| Branch | `main` |
| Build Command | `python3 fpl_liga.py --build public` |
| Publish Directory | `public` |
| Environment Variable | `FPL_LEAGUE` = linket fra draft-siden, fx `https://draft.premierleague.com/entry/123456/event/5` |

### 3. Automatisk opdatering
1. I Render: **Settings → Deploy Hook** → kopier URL'en.
2. I GitHub: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `RENDER_DEPLOY_HOOK`
   - Secret: URL'en fra Render
3. Test under **Actions → Opdater data → Run workflow**.

Opdater med det samme: kør workflowet manuelt, eller vælg *Manual Deploy* i Render.

Bemærk:
- Hvis et build fejler (fx fordi FPL er nede under opdatering), beholder Render den forrige version.
- GitHub slår planlagte workflows fra, hvis der ikke har været aktivitet i repoet i 60 dage.
  Så skal du bare slå det til igen under Actions.
- Siden er offentlig for alle med linket og viser navne og holdnavne fra ligaen.
