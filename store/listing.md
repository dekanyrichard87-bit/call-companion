# Chrome Web Store – áruházi adatok (Call Companion – MiniCRM kapcsolat)

Bemásolható szövegek a Chrome Web Store fejlesztői irányítópultjához (Developer Dashboard), mezőnként.
A feltöltendő csomag: `dist\CallCompanion-extension-1.0.0.zip` (a `build.ps1` készíti).

## Csomag (Package)
- **Fájl:** `CallCompanion-extension-<verzió>.zip`
- A manifest verziója `1.0.0`, ennél régebbit az áruház nem fogad el később sem: minden új feltöltéshez nagyobb
  `version` kell.

## Áruházi adatlap (Store listing)
- **Nyelv:** magyar
- **Név:** Call Companion – MiniCRM kapcsolat *(a manifestből jön)*
- **Összefoglaló (max. 132 karakter):** Átadja a MiniCRM-munkamenetet a gépen futó Call Companion programnak
  (csak a 127.0.0.1 címre). *(a manifestből jön)*
- **Leírás:**

> A Call Companion Windows-programhoz tartozó bővítmény. A program a Bria Enterprise telefonon érkező és indított
> hívásoknál azonnal megmutatja, ki hív: a nevét és a cégét a MiniCRM-ből, egy kis kártyán vagy a főablakban.
>
> Ehhez a programnak nem kell külön jelszó vagy API-kulcs: ez a bővítmény átadja neki a Chrome-ban már
> bejelentkezett MiniCRM-munkamenetet.
>
> Amit a bővítmény csinál:
> • a r3.minicrm.hu munkamenet-sütijeit átadja a gépen futó Call Companion programnak – kizárólag a 127.0.0.1 címre;
> • félpercenként ellenőrzi, hogy fut-e a program, és újraindítás után magától újra átadja a munkamenetet;
> • az eszköztár-ikonon piros „!” jelzi, ha a program nem érhető el; a felugró ablakban látszik az állapot és van egy
>   „Szinkron most” gomb.
>
> Amit nem csinál: nem olvassa a MiniCRM oldalak tartalmát, nem fér hozzá más weboldalakhoz, nem küld adatot
> semmilyen külső szerverre, nincs benne analitika vagy követés.
>
> A bővítmény önmagában nem használható: a Call Companion programmal együtt működik.

- **Kategória:** Munkahelyi eszközök / Termelékenység › Munkafolyamat és tervezés (*Productivity › Workflow &
  Planning*)
- **Ikon (128×128):** `extension/icons/icon128.png`
- **Képernyőkép (1280×800, legalább 1):** lásd lent, „Képernyőkép készítése”.
- **Kis promóciós csempe (440×280):** `store/promo-440x280.png`
- **Webhely / támogatás:** https://github.com/dekanyrichard87-bit/call-companion

## Adatvédelem (Privacy practices)
- **Egyetlen cél (Single purpose):**
  > A bejelentkezett MiniCRM-munkamenetet átadja az ugyanazon a gépen futó Call Companion asztali programnak
  > (127.0.0.1), hogy az a MiniCRM-ből kikereshesse a hívó nevét.
- **Jogosultságok indoklása:**
  - `cookies`: A r3.minicrm.hu munkamenet-sütijeinek kiolvasásához, hogy a gépen futó Call Companion program a
    felhasználó meglévő bejelentkezésével kereshessen a MiniCRM-ben. A sütik csak a 127.0.0.1 címre kerülnek.
  - `alarms`: Félpercenként ellenőrzi, hogy fut-e a Call Companion program, és ha újraindult, újra átadja a
    munkamenetet; sikertelen átadás után ezzel próbálkozik újra.
  - **Gazdagép-engedélyek (host permissions):**
    - `https://r3.minicrm.hu/*`: a MiniCRM sütijeihez, és egy tartalom-szkripthez, amely csak „felébreszti” a
      bővítményt (az oldal tartalmát nem olvassa).
    - `http://127.0.0.1/*`: a gépen futó Call Companion programmal való kapcsolathoz (port 48653).
- **Távoli kód (Remote code):** Nem, nem használok távoli kódot.
- **Adatgyűjtés:** „Hitelesítési adatok” (*Authentication information*) – a MiniCRM-munkamenet sütijei. Csak a
  helyi gépen futó programnak kerülnek átadásra, nem tárolja és nem küldi máshová.
- **Tanúsítások (mind a hármat be kell pipálni):** nem adom el / nem adom tovább harmadik félnek; nem használom a
  bővítmény céljától eltérő célra; nem használom hitelképesség megállapítására.
- **Adatvédelmi irányelvek URL-je:** https://dekanyrichard87-bit.github.io/call-companion/privacy.html

## Terjesztés (Distribution)
- **Láthatóság:** Nem nyilvános (*Unlisted*) – csak a linkkel található meg.
- **Régiók:** Magyarország (vagy minden régió).

## Képernyőkép készítése (1280×800)
1. Chrome-ban nyisd meg a MiniCRM-et, a Call Companion fusson.
2. Kattints az eszköztáron a Call Companion ikonra: megnyílik a felugró ablak (*Kapcsolódva ✓*).
3. Win + Shift + S, téglalap kijelölés: a felugró ablak és körülötte a MiniCRM-oldal (ügyfélnév ne látsszon,
   vagy takard ki).
4. Illustratorban / Photoshopban helyezd egy 1280×800-as vászonra (például a Call Companion sötét háttérrel,
   #1e1e2e), mentsd PNG-ként: `store/screenshot-1280x800.png`.
