-- =====================================================================
-- CuidarMéx · Modelo de planeación (se ejecuta UNA vez; es seguro repetirlo)
--   Adultos mayores (60+)                : dato por alcaldía
--   Personas que requieren cuidado       : 15 % de los adultos mayores
--   Cuidadores recomendados (requeridos) : 1 por cada 5 personas
--   Oferta actual                        : oferta estimada base + cambios por uso de la app
--   Déficit %                            : (recomendados - oferta) / recomendados
--   Semáforo: Rojo > 30 %, Amarillo 15-30 %, Verde < 15 %
-- Cifras de trabajo del proyecto (estimaciones, no estadísticas oficiales).
-- =====================================================================
BEGIN;

ALTER TABLE demanda_alcaldia ADD COLUMN IF NOT EXISTS oferta_base      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE demanda_alcaldia ADD COLUMN IF NOT EXISTS solicitudes_base INTEGER NOT NULL DEFAULT 0;
ALTER TABLE demanda_alcaldia ADD COLUMN IF NOT EXISTS modelo           VARCHAR(20) NOT NULL DEFAULT '';

-- Línea base: lo que ya hay en la app al momento de aplicar el modelo
-- (así la tabla arranca exactamente con las cifras de planeación y luego cambia con el uso).
WITH oferta(alcaldia, adultos, oferta) AS (VALUES
  ('Iztapalapa', 280000, 4500), ('Gustavo A. Madero', 220000, 3800), ('Coyoacán', 165000, 4000),
  ('Álvaro Obregón', 145000, 3200), ('Tlalpan', 135000, 3000), ('Cuauhtémoc', 105000, 2800),
  ('Azcapotzalco', 100000, 1900), ('Venustiano Carranza', 92000, 1700), ('Miguel Hidalgo', 88000, 2500),
  ('Iztacalco', 82000, 1500), ('Xochimilco', 78000, 1200), ('Benito Juárez', 70000, 2000),
  ('Tláhuac', 65000, 900), ('La Magdalena Contreras', 42000, 800), ('Cuajimalpa de Morelos', 34000, 850),
  ('Milpa Alta', 20000, 250)
),
c AS (
  SELECT regexp_replace(regexp_replace(translate(lower(trim(alcaldia)),'áéíóúü','aeiouu'),' de morelos$',''),'^la ','') AS k,
         COUNT(*) AS n FROM caregivers GROUP BY 1
),
r AS (
  SELECT regexp_replace(regexp_replace(translate(lower(trim(alcaldia)),'áéíóúü','aeiouu'),' de morelos$',''),'^la ','') AS k,
         COUNT(*) AS n FROM requests GROUP BY 1
)
UPDATE demanda_alcaldia d SET
    adultos_mayores            = o.adultos,
    adultos_mayores_nota       = 'modelo de planeación',
    personas_requieren_cuidado = ROUND(o.adultos * 0.15),
    cuidadores_requeridos      = CEIL(ROUND(o.adultos * 0.15) / 5.0),
    oferta_base = CASE WHEN d.modelo = 'planeacion_v1' THEN d.oferta_base
                       ELSE GREATEST(0, o.oferta - COALESCE((SELECT n FROM c WHERE c.k =
                            regexp_replace(regexp_replace(translate(lower(trim(d.alcaldia)),'áéíóúü','aeiouu'),' de morelos$',''),'^la ','')),0)) END,
    solicitudes_base = CASE WHEN d.modelo = 'planeacion_v1' THEN d.solicitudes_base
                       ELSE COALESCE((SELECT n FROM r WHERE r.k =
                            regexp_replace(regexp_replace(translate(lower(trim(d.alcaldia)),'áéíóúü','aeiouu'),' de morelos$',''),'^la ','')),0) END,
    modelo = 'planeacion_v1',
    fuente = 'Modelo de planeación del proyecto (15 % de adultos mayores; 1 cuidador por 5 personas). No oficial.'
FROM oferta o WHERE d.alcaldia = o.alcaldia;

-- El modelo trabaja con adultos mayores: una sola categoría coherente con los totales
DELETE FROM demanda_categoria;
INSERT INTO demanda_categoria (alcaldia, categoria, personas)
SELECT alcaldia, 'Persona adulta mayor', personas_requieren_cuidado FROM demanda_alcaldia;

COMMIT;
