# Runbook — Support N2 du robot « Factures fournisseurs »

## Qui traite quoi

| Situation | Statut dans la file | Qui | Action |
|---|---|---|---|
| Facture non conforme (`BE_*`) | Failed / BusinessException | **Métier** (comptabilité fournisseurs) | Rapport Excel « À traiter ». Le support ne relance **pas** |
| Panne passagère de l'ERP | Retried → Successful | Personne | L'Orchestrator a relancé tout seul |
| Échec système après toutes les relances | Failed / ApplicationException | **Support N2** | Procédure A |
| Robot arrêté en cours de transaction | Abandoned | **Support N2** | Procédure B |
| Robot arrêté pour exceptions consécutives | journal `Fatal` | **Support N2** | Procédure C |

## Contrôle quotidien

```powershell
.\ops\Get-QueueHealth.ps1
```
Code de sortie `0` : rien à faire. `1` : procédure A. `2` : procédure B. Le script peut être planifié et
branché sur l'outil de supervision.

## Procédure A — échecs système après relances

1. Lire le détail : `Get-QueueHealth.ps1` affiche `ProcessingException.Details` (ex. `ERP timeout on POST /invoices`).
2. Identifier la cause dans les journaux du robot :
   ```powershell
   Get-Content data\logs\ROBOT-01.jsonl | ConvertFrom-Json | Where-Object level -eq 'Error' | Select-Object ts, file, message
   ```
3. Si la cause est l'ERP : vérifier qu'il répond (`Invoke-RestMethod http://127.0.0.1:8091/health`). S'il ne
   répond pas, escalader à l'équipe ERP et **ne rien relancer**.
4. Une fois la cause corrigée :
   ```powershell
   .\ops\Invoke-RetryFailedItems.ps1 -WhatIf   # ce qui sera relancé
   .\ops\Invoke-RetryFailedItems.ps1
   ```
   La relance est sans risque, même si la facture a été comptabilisée lors d'une tentative dont la réponse
   s'est perdue : la clé d'idempotence empêche une seconde écriture (voir [SDD §4](SDD.md#4-idempotence--le-point-délicat)).
5. Démarrer un robot (`python -m rpa_lab.performer --name ROBOT-N2`), puis relancer le contrôle quotidien :
   il doit sortir en `0`.

## Procédure B — éléments *Abandoned*

Un robot s'est arrêté (crash, redémarrage du poste) pendant une transaction. L'élément a pu être
comptabilisé ou non. Relancer avec `Invoke-RetryFailedItems.ps1` est sûr pour la même raison
d'idempotence. Chercher ensuite la cause de l'arrêt dans `data/logs/` et dans les journaux système.

## Procédure C — robot arrêté (Fatal)

Après 5 exceptions système consécutives, le robot s'arrête au lieu de vider la file en échecs. Corriger
la cause (en général l'ERP), puis redémarrer le robot. Les éléments restés *New* seront traités
normalement.

## Message d'escalade type (équipe ERP)

> Objet : [RPA Factures] ERP indisponible — N transactions en échec depuis HH:MM
> Le robot reçoit des erreurs `503` et des délais dépassés sur l'API factures depuis HH:MM (détail joint).
> Aucune double comptabilisation : les relances sont idempotentes. Les N factures concernées seront
> relancées dès votre retour. Merci de nous prévenir quand l'API est rétablie.
