-- =====================================================================
-- CuidarMéx · Simulación: agregar cuidadores a UNA alcaldía (para la exposición)
-- Uso (psql):
--   psql -U postgres -h localhost -d cuidarmex -v alcaldia="Cuajimalpa de Morelos" -v cantidad=18 -f fase2b_cuidadores_simulados.sql
-- Cada cuidador queda con origen = 'simulacion_v1' (se quitan con fase2c_limpiar_demo.sql).
-- A diferencia de los pacientes de demostración, ESTO SÍ mueve el semáforo (es el escenario).
-- Referencia (desde la tabla del proyecto): Cuajimalpa +18 -> verde; Magdalena Contreras +82,
-- Milpa Alta +170, Azcapotzalco +200 -> amarillo; Coyoacán +208 -> verde.
-- =====================================================================
BEGIN;
ALTER TABLE caregivers ADD COLUMN IF NOT EXISTS origen VARCHAR(30);

WITH centros(alcaldia, lat, lng) AS (VALUES
  ('Álvaro Obregón',19.359,-99.203),('Azcapotzalco',19.487,-99.184),('Benito Juárez',19.373,-99.158),
  ('Coyoacán',19.346,-99.162),('Cuajimalpa de Morelos',19.357,-99.295),('Cuauhtémoc',19.433,-99.154),
  ('Gustavo A. Madero',19.484,-99.113),('Iztacalco',19.395,-99.097),('Iztapalapa',19.355,-99.062),
  ('La Magdalena Contreras',19.307,-99.241),('Miguel Hidalgo',19.432,-99.200),('Milpa Alta',19.192,-99.023),
  ('Tláhuac',19.287,-99.005),('Tlalpan',19.287,-99.168),('Venustiano Carranza',19.424,-99.105),
  ('Xochimilco',19.257,-99.103))
INSERT INTO caregivers
  (nombre, experiencia, costo_hora, especialidad, calificacion, total_resenas,
   alcaldia, latitud, longitud, ubicacion_ajustada, origen)
SELECT
  (ARRAY['Ana','Laura','Carlos','Miguel','Sofía','Daniel','Paola','Jorge','Lucía','Andrés','Karla','Héctor',
         'Mónica','Iván','Rosa','Diego'])[1+floor(random()*16)::int] || ' ' ||
  (ARRAY['Ramos','Mendoza','Castillo','Ortega','Salazar','Navarro','Herrera','Campos','Vega','Silva',
         'Aguilar','Ríos'])[1+floor(random()*12)::int] || ' ' ||
  (ARRAY['Luna','Soto','Peña','Cortés','Maldonado','Guerrero','Medina','Fuentes','Delgado','Rojas',
         'Padilla','Lara'])[1+floor(random()*12)::int],
  (1 + floor(random()*12))::int || ' años',
  (ARRAY[100,120,140,160,180,200])[1+floor(random()*6)::int],
  (ARRAY['Persona adulta mayor','Persona adulta mayor','Persona adulta mayor','Acompañamiento',
         'Cuidado personal','Persona con discapacidad'])[1+floor(random()*6)::int],
  0, 0,
  c.alcaldia,
  c.lat + (random() - 0.5) * 0.04,
  c.lng + (random() - 0.5) * 0.04,
  TRUE,
  'simulacion_v1'
FROM centros c, generate_series(1, :cantidad) g
WHERE c.alcaldia = :'alcaldia';

SELECT COUNT(*) AS cuidadores_simulados_agregados FROM caregivers WHERE origen = 'simulacion_v1';
COMMIT;
