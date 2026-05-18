# Top Rules

The following project rules apply to this service and to the document suite. Source: top_rules.md — that file must not be changed.

**Rule 1.** You may not change anything in top_rules.md.

**Rule 2.** top_rules.md sets project and limitations. The following files must exist where required: readme.md (deployment plan); progress.md (progress tracking); newtask.txt (debugging focus); changed_files.md (tracking edited files). Create any of these if they do not exist, or ensure the deployment plan includes a step to create them.

**Rule 3.** readme.md contains the deployment plan numbered as 1.a, 1.b, etc.

**Rule 4.** progress.md lists progress after each step in the deployment plan (readme.md), including detailed results of tests created for any coded function. Use pytest. Use the same numbering as readme.md. Do not suggest advancing before all tests are cleared. If tests are blocked (e.g. by environment or database), document the blocker in progress.md and suggest how to unblock. After each step, check that all rules are followed, update progress.md, and include a list of the tests deployed and the result of each test.

**Rule 5.** initial_sql_design.txt holds a suggested design of the database; use only as reference.

**Rule 6.** (left this blank)

**Rule 7.** Use Docker to contain the application fully. Use virtual environment where applicable

**Rule 8.** The application will need API keys: include suggested rules (storage, rotation, expiry, revocation) and procedures and maintenance in the deployment plan (readme.md).

**Rule 9.** When appropriate, create a script that tests all API endpoints according to the capability statement.

**Rule 10.** The local database is based on FLASK and PostgreSQL and is localhost to begin with.

**Rule 11.** All results of tests etc. are stored in ./results/<timestamp>results/ (ISO-8601 UTC; e.g. 2026-02-19T14-30-00Z_results).

**Rule 12.** Whenever something is changed on the web level: first download what is to be changed to a temporary archive, then compare with what is locally available. Then, without coding anything, present the result of the comparisons and suggest the next step. The result should always be that we have the same locally as on the web. All transfer and management on the web is done by the operator based on instructions. No ssh or scp from the plan.

**Rule 13.** (Later in project.) Present focus is given in newtask.txt. Create it if it does not exist when needed (debugging phase). This file is an extension of the readme.

**Rule 14.** Maintain GIT structure separately but do not touch folder _obs_gateway_repo

**Rule 15.** Put priority on being fully compliant with FHIR 5. FHIR compliance is enforced for API schema, DB model, capability statement and validation layer.

**Rule 16.** Assume ownership of ports 9050–9053 on localhost. Use only those ports. Starting the database and other applications must be collected in a single bash script (./start.sh): kill previously used project ports (9050–9053), activate venv, start the DB and app; on Ctrl+C gracefully shut down and deactivate. See to that docker is started properly or is running when activating the application.

**Rule 17.** Note in changed_files.md all edited files from now on, with full path.

**Rule 18.** For robustness: all internal traffic should be guided by references to GUIDs whenever possible. All matching of activities/transactions/goals must use GUID, not ID. Frontend communication is based on GUIDs. Backend always refers to GUIDs.

**Rule 19.** The operator does all editing on the web application. You prepare scripts for updating the web instance following analysis. When restart is necessary, run safe_restart.sh on the web instance (the script is prepared as part of the plan or is present on the web instance).

**Rule 20.** Create a script that tests all API endpoints according to the capability statement.

**Rule 21.** Keep the created app in a separate folder including its venv and database. Make sure to update the requirements.txt file with the dependencies of the app.Keep the root clean.

**Rule 22.** The future implementation on the server is fragile and all precaution must be taken to prevent disturbance of other services in the reverse proxy

**Rule23** the .env must be fully prepared and boot strap SU user must be possible to create in the first implementation on the server (macmini). Development is done on tha local MAC.

 **Rule24** Use documents in ../css_instrux i.e. the pdhc_markdown_layout_standard.md and repo.css.md for all frontend designs. Copy pdhc.css into each new repo's static assets

 ** Rule 25** Add a CLAUDE.md in each repo that says: "Follow the design system in repo_css.md. Base font 12px, use the PDHC colour tokens, extend base.html.

 *General Description*

 Beskrivning av behöver, vi har ju då gatewayen, jag vill ge lite kontext nu för att förstå. Gatewayen som finns, den uppgift är att ta emot observationer från ett externt system som vi kallar för provider i den bilden som du kommer få bifogat med det här meddelandet. Det vi ska bygga nu är en request-tjänst vars syfte är att ett, det är att godkända providers får rätten att prenumerera via webhooks och FIRES subscription, vill jag att du använder, för att de vill prenumerera på beställningar som kommer från sjukvården i det här use caset. Det kan vara andra också, det kan vara hälso- och sjukvård, det kan vara friskvård. När de prenumererar på det här så kommer de få en avisering om att nu finns det en ny request, det vill säga att det finns en person eller patient, beroende på hur det sägs, som ska engageras med en care plan som är specifikt för den personen. Så, vad innebär det? Jo, det innebär då att när jag hämtar via ett annat API, som inte är skrivkärn-API, utan då hämtar jag med hjälp av en token som jag har fått i min kontraktsförhandling med tjänsteleverantören. Så har jag rätten att då hämta ut den här beställningen. Och det är en så kallad request för någonting. Och vad består requesten av? Jo, den består av det viktigaste då, det är patienten som ska engageras. Den består av metadata, kanske kring den, som är relevant. Den består definitivt av en vårdplan, och den vårdplanen har en koppling till en plandefinition. Så plandefinitionen, som då Doktorn i det här fallet har iakttagit på patienten, blir då en instansierad plandefinition, blir då vad som kallas för en fire care plan. Det tillsammans med den kontraktuella relationen mellan provider och sjukvården beskrivs i form av ett provider ID, med hur länge det här avtalet är giltigt, det vill säga att det finns ett avtal. Förmodligen kommer man inte få någon skrivkärn-avisering om man inte har ett giltigt avtalsperiod. Och sedan vilka plandefinitioner som omfattas av det här avtalet för att hålla ordning och reda på det här. Och sedan lite mer juridiska relationer. Allt det här byggs ihop i ett kontraktsobjekt som består av mänskligt, personläsbart avtal som är påskrivet, men också för datamaskin en någon format JSON beskrivning av den här relationen med de parametrar som jag har sagt. Det ligger till grund för requests-tjänstens validering av att den här providern får ta ut den här datan. Providern kan känna sig trygg med att den careplanen får, vet hur den ska hantera, för då har man ju då avtalsmässigt bestämt att den här plandefinitionen ska gälla. Och då gifter vi ihop det, så när då providern enkelt skickar in en mätpunkt som då man har fördefinierat token så kommer den kunna maxa tillbaka från gatewayen upp till requesten, upp till kontraktet och hela vägen tillbaka till Doktorns careplan och plandefinition där spårvagnen finns. Så att det som är till uppdraget för den som hämtar ut, som harvestar datan från gatewayen och CDR är att göra den här ihopkopplingen, så det behövs byggas andra typer av interna API för att kunna göra ut det här på något sätt. Men det blir nästa uppdragsbeskrivning som ni ska bygga med Challenge Cursor. Det är perfekt. 

 tilläggsuppdrag: 
 1. När data kommer in ska kvitto skickas för lagring hos provider. Ta fram ett protokoll för det och intreuktioner för omkodning av Provider så den kan ta emot kvitton. Kvitto skickas också på data som inte accepterats och då flaggas för icke accepterade.

 2. en sida som listar inkomna data skall göras provisoriskt där man kan klicka på datapunkten och få bakomliggande information, speca att kvitto är utskickat, patient guid, conceptnamn, provider namn och time received. Kunna filtrera på patientguid.

 3. FHIR formatet för mottagande av data gäller fhir form responses, FHIR observations och patientkarakteristik och metadata. Förhandla fram så att data mottagandet stämmer med data utskicket från Provider. Ta fram instruktioner 

 4. Nedladdningsbara manualer non tech och teknisk dokumentation, api dokumentation och autentiseringsprocedur

 5. Gå över och titta på ../provider1. orientera dig och se vad som behöver göras för att dataöverföreing ska ske automatiskt och dokumenterat

 6. Kontrollera att all datatrafik stämmer med FHIR 5

7. Gör en funktion som noterar när en request är komplett levererad eller har avslutats på grund av tid. Lista på en särskild websida. Utvidga databasen så denna informationen kan listas








