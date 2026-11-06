# How this lab maps to UiPath and Power Automate

This repository is written in Python so it runs anywhere with free tools. It contains **no UiPath XAML and
no Power Automate flow export**. Every component was designed to follow the UiPath contract, so porting it
is a matter of translating each component, not redesigning it. The table below is that translation.

| Lab component | UiPath | Power Automate / Microsoft 365 |
|---|---|---|
| `rpa_lab/orchestrator.py` — queue API | Orchestrator Queue: *Unique Reference*, *Auto Retry*, *Max # retries* | Dataverse or SharePoint list used as a work queue, or Power Automate *work queues* |
| `AddQueueItem` / `StartTransaction` / `SetTransactionResult` | Activities *Add Queue Item*, *Get Transaction Item*, *Set Transaction Status* | *Add a row* / *Update a row* on the work-queue table |
| `rpa_lab/dispatcher.py` | Dispatcher process: *For Each File in Folder* + *Add Queue Item* | Trigger *When a file is created in a folder* (SharePoint / OneDrive) |
| `rpa_lab/performer.py` state machine | REFramework template (Init, Get Transaction Data, Process, End) | Cloud flow with *Scope* blocks Try / Catch / Finally |
| `BusinessRuleException` / `ApplicationException` | Same names, same semantics in REFramework | Catch scope that branches on the error type |
| `config/settings.json` + `RPA_*` overrides | `Data/Config.xlsx` + Orchestrator Assets | Environment variables + Azure Key Vault / connection references |
| `rpa_lab/document.py` (Tesseract + rules) | Document Understanding: *Digitize Document* → *Extract Document Data* (Regex / Form extractor) | AI Builder *Extract information from invoices* |
| confidence < threshold → `BE_EXTRACTION_REVIEW` | *Present Validation Station* / Action Center task | *Start and wait for an approval* |
| `rpa_lab/ai_fallback.py` (local LLM) | Generative extractor / *GenAI activities* | AI Builder GPT prompt |
| ERP REST calls + `Idempotency-Key` | *HTTP Request* activity with a header | HTTP connector / custom connector |
| `rpa_lab/exceptions_report.py` (Excel) | *Excel Application Scope* / *Write Range*, sent by *Send Outlook Mail* | *Create table* in Excel Online + *Send an email (V2)* |
| `ops/*.ps1` (L2 support) | Orchestrator UI or Orchestrator API from PowerShell | Same scripts against the Dataverse Web API |

## What porting would involve

- **UiPath**: start from the REFramework template and replace `Process.xaml` with the checks from
  `performer.py` (RG-01 to RG-07). Configure the queue with *Unique Reference* and 2 retries. Use Document
  Understanding for OCR and Action Center for the review step.
- **Power Automate**: the dispatcher becomes a SharePoint trigger. The ERP checks become HTTP actions. The
  review step becomes an approval in Teams.

None of this is done here. It is the next step, and it requires a UiPath Community account or a Microsoft
365 tenant.
