# SDD — Conception de la solution

*Solution Design Document du processus décrit dans le [PDD](PDD.md).*

## 1. Architecture

```
 dossier partagé ──> Dispatcher ──AddQueueItem──> Orchestrator (file FACTURES_FOURNISSEURS)
                                                      │ StartTransaction (1 élément = 1 robot)
                                       ┌──────────────┴──────────────┐
                                  ROBOT-01                       ROBOT-02        (unattended, en parallèle)
                                       │  OCR Tesseract + règles d'extraction (+ LLM local en secours)
                                       │  contrôles RG-01..RG-07
                                       └──> API ERP (référentiel, factures, commandes) ── Idempotency-Key
                                                      │
                                    SetTransactionResult (Successful / Business / Application)
                                                      │
                     rapport Excel métier  <──────────┴──────────>  scripts PowerShell support N2
```

## 2. Modèle Dispatcher / Performer

- **Dispatcher** (`rpa_lab/dispatcher.py`) : une passe sur le dossier. `Reference` = 32 premiers caractères
  du SHA-256 du fichier. Un même scan déposé deux fois reçoit un 409 de l'Orchestrator : il n'est traité
  qu'une fois.
- **Performer** (`rpa_lab/performer.py`) : machine à états du REFramework.

| État REFramework | Implémentation | Rôle |
|---|---|---|
| Init / `InitAllApplications` | `init_all_applications()` | Lit la config, ouvre les sessions Orchestrator et ERP, contrôle de santé |
| Get Transaction Data | `get_transaction()` | `StartTransaction` ; 204 → End |
| Process | `process()` | OCR, extraction, contrôles, comptabilisation |
| Set Transaction Status | `set_status()` | `SetTransactionResult` avec le type d'exception |
| Exception système | `except Exception` | Journalise, ferme et rouvre les applications, compte les échecs consécutifs |
| End / `CloseAllApplications` | `close_all_applications()` | Arrêt propre ; arrêt forcé après 5 exceptions système consécutives |

## 3. File d'attente

| Paramètre | Valeur | Raison |
|---|---|---|
| Unique Reference | Oui | Anti-doublon à l'entrée |
| Auto Retry | Oui, `QueueMaxRetryNumber = 2` | Les pannes ERP sont transitoires |
| Retry sur exception métier | Non | Relancer une facture fausse ne la corrige pas |
| Délai *InProgress* → *Abandoned* | 300 s | Détecte un robot planté en cours de transaction |
| Priorité | Normal (High disponible) | Ex. factures avec escompte à échéance proche |

## 4. Idempotence : le point délicat

Un `POST /invoices` peut **réussir côté ERP alors que la réponse se perd** (délai dépassé côté robot).
Le robot lève une exception système et l'Orchestrator relance l'élément. Sans précaution, la relance :
- soit comptabilise une seconde fois ;
- soit tombe sur sa propre écriture et la signale à tort comme doublon (`BE_DUPLICATE_INVOICE`).

Solution en deux parties :
1. La `Reference` de l'élément (identique pour toutes les relances) est envoyée comme en-tête
   `Idempotency-Key`. L'ERP renvoie l'écriture existante au lieu d'en créer une nouvelle.
2. Le contrôle de doublon (RG-05) reconnaît une écriture portant **sa propre** clé : il considère alors la
   transaction comme terminée, et non comme un doublon.

Le laboratoire injecte ce cas exprès (`RPA_ERP_LOST_RESPONSE`). Les rapports comptent les réponses perdues
résolues sans double comptabilisation.

## 5. Lecture des documents et IA

- OCR : Tesseract 5, modèle `fra`, en mode TSV pour obtenir **une confiance par mot**. La confiance d'un
  champ est celle des mots de la *valeur*, pas de son libellé : un « Émise le » mal lu ne doit pas rendre
  la date suspecte.
- Extraction : expressions régulières tolérantes aux erreurs d'OCR courantes (`P0-` → `PO-`, séparateurs
  de milliers, `20 %` retiré avant de lire un montant).
- Secours IA (option `--ai-fallback`) : si des champs manquent ou sont invalides, le texte OCR est envoyé à
  un LLM local (Ollama, `qwen2.5:7b`, température 0, sortie JSON). **Seuls les champs manquants sont
  remplacés**, puis les mêmes contrôles RG-01 à RG-07 s'appliquent. Un montant halluciné échoue au
  contrôle arithmétique ou au rapprochement de commande : la facture part en contrôle humain. Les données
  ne quittent pas le poste.

## 6. Paramétrage

`config/settings.json` joue le rôle de `Data/Config.xlsx`. Chaque clé peut être surchargée par une variable
d'environnement `RPA_<CLE>`, comme un *asset* Orchestrator. En production, les URL et identifiants seraient
des *assets* (identifiants de type *Credential*), jamais stockés en clair dans le projet.

## 7. Journalisation

Journaux JSON (un fichier par robot, `data/logs/<robot>.jsonl`). Champs ajoutés à chaque transaction,
comme *Add Log Fields* : `transaction_id`, `reference`, `file`, `retry`. Niveaux Info, Warn (exception
métier), Error (exception système) et Fatal (arrêt du robot).

## 8. Tests

`pytest` (21 tests) : contrat de la file (unicité, priorité, un élément = un robot, relances, abandon),
idempotence de l'ERP, analyse des montants et dates, contrôle de Luhn, confiance au niveau de la valeur.
Recette de bout en bout : `scripts/run_lab.py`, comparée à la vérité terrain.
