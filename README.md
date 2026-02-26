# sismocan

Mapa interactivo de sismicidad en las Islas Canarias, actualizado automáticamente.

🌐 **[sismocan.github.io](https://sismocan.github.io)** *(próximamente)*

---

## ¿Qué es?

**sismocan** muestra en un mapa todos los seísmos registrados en el archipiélago canario desde 2015. Los datos vienen del catálogo oficial del **IGN** (Instituto Geográfico Nacional) y se actualizan automáticamente cada 5 minutos.

No requiere instalación, ni cuenta, ni pago de ningún tipo. Es una página web estática alojada en GitHub Pages.

---

## ¿Qué puedes hacer?

- Ver dónde y cuándo ocurrieron los seísmos en el mapa
- Filtrar por período (últimas 24 h, 3 días, 7 días, 30 días, último año o todo el histórico)
- Filtrar por magnitud mínima con un deslizador o escribiendo el valor exacto
- Pinchar en cualquier marcador para ver los detalles: magnitud, profundidad, fecha/hora y localización
- Los puntos se agrupan automáticamente al alejar el zoom y se separan al acercarte

---

## Datos

Los eventos sísmicos proceden del **[Catálogo de Terremotos del IGN](https://www.ign.es/web/ign/portal/sis-catalogo-terremotos)**, que es la fuente oficial para la sismicidad en España. El catálogo cubre las Islas Canarias desde enero de 2015 con todas las magnitudes registradas.

---

## Ejecutar en local

Solo necesitas Python instalado:

```bash
git clone https://github.com/miqueas-gg/sismocan.git
cd sismocan
pip install -r requirements.txt
python -m http.server 8000
```

Abre `http://localhost:8000` en el navegador.

Para actualizar los datos manualmente:

```bash
python scripts/fetch_quakes.py
```

---

## Contribuir

Cualquier mejora es bienvenida. Abre un issue para comentar la idea antes de ponerte a programar, o manda directamente un Pull Request contra la rama `develop`.

---

## Licencia

[MIT](LICENSE) — libre para usar, modificar y distribuir.

---

*Datos: [IGN — Instituto Geográfico Nacional](https://www.ign.es) · Mapa: [OpenStreetMap](https://www.openstreetmap.org)*
