# Monitor mercati settimanale

Agente che ogni **venerdì alle 22:30 (ora italiana)** scarica le chiusure della settimana di
azioni, ETF, obbligazioni e commodities, calcola indicatori e un punteggio per ogni titolo e
aggiorna una dashboard HTML pubblicata online. Tutto gira da solo su GitHub, gratis, anche a PC spento.

```
config/tickers.yaml                  ← elenco titoli (modificabile)
scripts/fetch_data.py                ← agente: scarica, calcola, salva
.github/workflows/...yml             ← pianificazione venerdì 22:30
docs/index.html                      ← dashboard
docs/data.js                         ← dati della dashboard (generato)
data/storico_settimanale.csv         ← storico prezzi settimanali (apribile con Excel)
data/report_settimanale.csv          ← indicatori dell'ultima settimana
data/archivio/report_AAAA-MM-GG.csv  ← una fotografia per ogni settimana chiusa
```

Al primo avvio la dashboard contiene **dati dimostrativi** (con un banner che lo segnala):
vengono sostituiti dai dati reali al primo aggiornamento.

---

## Messa in funzione (circa 10 minuti, una volta sola)

1. **Crea un account** gratuito su <https://github.com> (se non ce l'hai).
2. **Crea un repository**: in alto a destra **+ → New repository**
   - Nome: `monitor-mercati`
   - Visibilità: **Public** (GitHub Pages gratuito richiede un repository pubblico; contiene solo dati di mercato pubblici)
   - Clic su **Create repository**.
3. **Carica i file**: nella pagina del repository clic su **uploading an existing file**, poi trascina
   **tutto il contenuto** della cartella `borsa-dashboard` (non la cartella stessa).
   ⚠️ Deve esserci anche la cartella nascosta `.github`. Su Mac premi `Cmd + Shift + .` nel Finder
   per vederla; su Windows: Esplora file → Visualizza → Elementi nascosti.
   Clic su **Commit changes**.
4. **Permessi dell'agente**: **Settings → Actions → General → Workflow permissions** →
   seleziona **Read and write permissions** → **Save**.
5. **Pubblica la dashboard**: **Settings → Pages** → *Source*: **Deploy from a branch**,
   *Branch*: **main**, cartella **/docs** → **Save**.
6. **Primo aggiornamento con dati reali**: scheda **Actions** → **Aggiornamento settimanale** →
   **Run workflow** → **Run workflow**. Dopo 2-3 minuti la spunta diventa verde.
7. **Apri la dashboard** all'indirizzo `https://<tuo-utente>.github.io/monitor-mercati/`
   (lo trovi anche in Settings → Pages). Salvalo nei preferiti.

Da quel momento l'aggiornamento è automatico ogni venerdì sera.

---

## Uso quotidiano

- **Aggiungere/togliere titoli**: apri `config/tickers.yaml` su GitHub, icona ✏️, modifica e
  *Commit changes*. Il ticker è quello di Yahoo Finance (cerca il titolo su finance.yahoo.com:
  per Borsa Italiana il suffisso è `.MI`, es. `ENI.MI`). Per vederlo subito: *Run workflow*.
- **Se un aggiornamento fallisce** GitHub ti manda una email. Le cause tipiche sono un blocco
  temporaneo di Yahoo Finance: basta rilanciare con *Run workflow*. Se meno del 50% dei titoli
  viene scaricato, l'agente **non** sovrascrive i dati della settimana precedente.
- **Titoli non trovati** o dati vecchi sono elencati in fondo alla dashboard, nel riquadro *Avvisi*.
- **Scaricare i dati**: pulsante *Scarica CSV* nella dashboard, oppure i file nella cartella `data/`.

## Prova sul proprio PC (facoltativo)

```bash
pip install -r requirements.txt
python scripts/fetch_data.py          # dati reali
python scripts/fetch_data.py --demo   # dati simulati
```
poi apri `docs/index.html` nel browser.

## Metodologia

| Indicatore | Calcolo |
|---|---|
| Variazioni | 1, 4, 12, 26, 52 settimane e da inizio anno, su chiusure del venerdì |
| RSI | 14 periodi settimanali (Wilder) |
| Volatilità | deviazione standard dei rendimenti settimanali delle ultime 26 settimane, annualizzata |
| Trend | rialzista se prezzo > media 10 sett. > media 30 sett.; ribassista se l'opposto; altrimenti laterale |
| Punteggio 0-100 | percentile **all'interno dell'area**: 30% var. 12 sett., 20% var. 4 sett., 20% trend, 15% rendimento/rischio, 15% vicinanza al massimo a 52 sett.; −10 se RSI ≥ 75 |

Segnali: *Momentum forte*, *Ribasso in trend positivo*, *Ipercomprato*, *Ipervenduto*, *Debole*, *Neutro*
(definizioni in fondo alla dashboard).

Fonti: Yahoo Finance (prezzi), FRED – Federal Reserve Bank of St. Louis e BCE (rendimenti).
Strumento informativo: non costituisce consulenza finanziaria.
