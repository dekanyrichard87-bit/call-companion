# Újdonságok

Ezt a fájlt mutatja a program frissítés után („Újdonságok” ablak). Röviden, a felhasználónak szóló nyelven, verziónként
egy `## X.Y.Z` szakasz, alatta `- ` kezdetű pontok. A technikai részletek a CHANGELOG.md-ben vannak.

## 1.1.0
- **Rejtett szám:** ha a hívó letiltotta a számkijelzést, a kártya „Rejtett szám” feliratot mutat, és nem kínál keresést, másolást vagy visszahívást.
- **Nem fogadott hívások rendben:** csak a két legutóbbi kártya látszik teljes méretben, a régebbiek kis sávvá csukódnak. Kattintásra kinyílnak.
- **Magától eltűnik a nem fogadott kártya**, ha a hívóval később sikerült beszélni: újra hívott és felvetted, vagy visszahívtad és felvette. A hívásnaplóban ez a sor zöld megjegyzést kap.
- **A Bria értesítése már nem takarja el** a nem fogadott kártyát, amikor a hívás a főablakban csörög.
- **Továbbment hívások:** ha nem veszed fel, és a központ továbbviszi a hívást egy kollégádhoz, az nálad már nem nem fogadott hívás, hanem „Továbbment”. Beállítás: ⚙ Beállítások > Mellékek > Továbbadás → **20 mp**.
- **Áttekinthetőbb hívásnapló:** erősebb napfejlécek dátummal, és egy „Mind összecsuk / Mind kinyit” gomb. Összecsukva csak a napok látszanak a hívások számával.
- **Visszajelzés gomb** a főablakban (szövegbuborék ikon): hibát vagy ötletet egy kattintással elküldhetsz a készítőnek.
- **Újdonságok ablak:** frissítés után ez az ablak mutatja, mi változott. Később a tálcaikon menüjéből is elérhető.
- **Javítva: „Jelentkezzen be a MiniCRM-be”, pedig be voltál lépve.** Reggelente előfordult, hogy a program órákig nem talált neveket, és csak a MiniCRM-ből való ki- és bejelentkezés segített. Ezt a hibát megtaláltuk és kijavítottuk.
- **Gyorsabban zöld belépés után:** a MiniCRM egy friss belépést pár másodpercig még nem fogad el. A program ilyenkor fél percen belül magától újra próbálja. A bővítmény „Szinkron most” gombja is azonnal újraellenőriz.
