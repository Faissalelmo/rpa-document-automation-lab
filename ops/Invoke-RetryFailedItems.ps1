<#
.SYNOPSIS
    Support N2 : relance les éléments en échec SYSTÈME une fois la cause corrigée.

.DESCRIPTION
    Ne relance jamais une exception métier (une facture incohérente le restera : elle va au métier).
    Relancer est sans risque de double comptabilisation : l'élément relancé garde sa Reference, que le
    robot envoie à l'ERP comme clé d'idempotence. Si la facture avait déjà été enregistrée lors d'une
    tentative dont la réponse s'est perdue, l'ERP renvoie l'écriture existante au lieu d'en créer une.

    Supporte -WhatIf pour voir ce qui serait relancé.

.EXAMPLE
    .\ops\Invoke-RetryFailedItems.ps1 -WhatIf
    .\ops\Invoke-RetryFailedItems.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$OrchestratorUrl = "http://127.0.0.1:8090",
    [string]$QueueName = "FACTURES_FOURNISSEURS",
    [string]$ErpUrl = "http://127.0.0.1:8091"
)
$ErrorActionPreference = "Stop"

# 1. Never retry blind: the target system must be back first.
try {
    Invoke-RestMethod -Uri "$ErpUrl/health" -TimeoutSec 5 | Out-Null
} catch {
    Write-Error "L'ERP ne repond pas ($ErpUrl) : corriger l'incident avant de relancer."
    exit 1
}

$items = @(Invoke-RestMethod -Uri "$OrchestratorUrl/api/queues/$QueueName/items?status=Failed&exception_type=ApplicationException" | ForEach-Object { $_ })
if ($items.Count -eq 0) {
    Write-Host "Aucun element en echec systeme."
    exit 0
}
$retried = 0
foreach ($it in $items) {
    $label = "#$($it.Id) $($it.SpecificContent.FileName) (tentatives : $($it.RetryNumber + 1))"
    if ($PSCmdlet.ShouldProcess($label, "Retry")) {
        $new = Invoke-RestMethod -Method Post -Uri "$OrchestratorUrl/api/queues/$QueueName/items/$($it.Id)/retry"
        Write-Host "Relance : $label -> nouvel element #$($new.Id)"
        $retried++
    }
}
Write-Host "$retried element(s) relance(s). Demarrer un robot pour les traiter."
