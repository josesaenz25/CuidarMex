-- =====================================================================
-- CuidarMéx · Pacientes DE DEMOSTRACIÓN (datos 100 % sintéticos)
-- Requiere: fase1_migracion.sql y fase1c_modelo_planeacion.sql (la app la aplica sola).
-- Se ejecuta UNA vez (si ya existen, no duplica). Para quitarlos: fase2c_limpiar_demo.sql
--
-- Qué hace: crea solicitudes sintéticas repartidas según el semáforo del modelo.
--   Alcaldías en rojo     -> ~1.6 solicitudes pendientes por cada cuidador registrado
--   Alcaldías en amarillo -> ~1.1
--   Alcaldías en verde    -> ~0.6
--   + solicitudes completadas (~55 % de las pendientes) y asignadas (~8 %), para que
--     la tasa de éxito y las gráficas tengan historia.
-- No cambia el semáforo: suma lo insertado a solicitudes_base (línea base del modelo).
-- =====================================================================
BEGIN;

ALTER TABLE requests ADD COLUMN IF NOT EXISTS origen VARCHAR(30);

CREATE OR REPLACE FUNCTION cm_norm(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(regexp_replace(translate(lower(trim(t)),'áéíóúü','aeiouu'),' de morelos$',''),'^la ','')
$$;

DO $$
DECLARE
  n_antes INTEGER;
BEGIN
  IF EXISTS (SELECT 1 FROM requests WHERE origen = 'demo_pacientes_v1') THEN
    RAISE NOTICE 'Los pacientes de demostración ya existen; no se duplican.';
    RETURN;
  END IF;

  PERFORM setseed(0.37);
  SELECT COUNT(*) INTO n_antes FROM requests;

  CREATE TEMP TABLE _plan ON COMMIT DROP AS
  WITH centros(alcaldia, lat, lng) AS (VALUES
    ('Álvaro Obregón',19.359,-99.203),('Azcapotzalco',19.487,-99.184),('Benito Juárez',19.373,-99.158),
    ('Coyoacán',19.346,-99.162),('Cuajimalpa de Morelos',19.357,-99.295),('Cuauhtémoc',19.433,-99.154),
    ('Gustavo A. Madero',19.484,-99.113),('Iztacalco',19.395,-99.097),('Iztapalapa',19.355,-99.062),
    ('La Magdalena Contreras',19.307,-99.241),('Miguel Hidalgo',19.432,-99.200),('Milpa Alta',19.192,-99.023),
    ('Tláhuac',19.287,-99.005),('Tlalpan',19.287,-99.168),('Venustiano Carranza',19.424,-99.105),
    ('Xochimilco',19.257,-99.103)),
  cuid AS (SELECT cm_norm(alcaldia) AS k, COUNT(*) AS n FROM caregivers GROUP BY 1),
  base AS (
    SELECT c.alcaldia, c.lat, c.lng, COALESCE(u.n,0) AS cuidadores,
           (d.cuidadores_requeridos - (d.oferta_base + COALESCE(u.n,0)))::numeric
             / NULLIF(d.cuidadores_requeridos,0) * 100 AS deficit_pct
    FROM centros c
    JOIN demanda_alcaldia d ON cm_norm(d.alcaldia) = cm_norm(c.alcaldia)
    LEFT JOIN cuid u ON u.k = cm_norm(c.alcaldia)
  ),
  conteo AS (
    SELECT *, GREATEST(5, CEIL(cuidadores * CASE WHEN deficit_pct > 30 THEN 1.6
                                               WHEN deficit_pct >= 15 THEN 1.1 ELSE 0.6 END))::int AS pend
    FROM base
  )
  SELECT alcaldia, lat, lng, 'Pendiente'::text AS status, pend AS n FROM conteo
  UNION ALL SELECT alcaldia, lat, lng, 'Completado', ROUND(pend * 0.55)::int FROM conteo
  UNION ALL SELECT alcaldia, lat, lng, 'Asignada',   ROUND(pend * 0.08)::int FROM conteo;

  INSERT INTO requests
    (service_type, alcaldia, paciente, edad, prioridad, estado_salud, status,
     latitud, longitud, ubicacion_ajustada, caregiver_id, request_date, origen)
  SELECT
    CASE WHEN r < 0.70 THEN 'Persona adulta mayor' WHEN r < 0.82 THEN 'Acompañamiento'
         WHEN r < 0.92 THEN 'Cuidado personal' ELSE 'Persona con discapacidad' END,
    p.alcaldia,
    (ARRAY['María','José','Guadalupe','Juan','Francisca','Antonio','Margarita','Miguel','Rosa','Francisco',
           'Elena','Jesús','Teresa','Pedro','Carmen','Luis','Josefina','Javier','Leticia','Roberto',
           'Patricia','Ricardo','Alicia','Fernando','Silvia','Ernesto','Verónica','Raúl'])[1+floor(random()*28)::int]
    || ' ' ||
    (ARRAY['Hernández','García','Martínez','López','González','Pérez','Rodríguez','Sánchez','Ramírez','Cruz',
           'Flores','Gómez','Morales','Vázquez','Jiménez','Reyes','Díaz','Torres','Gutiérrez','Ruiz'])[1+floor(random()*20)::int]
    || ' ' ||
    (ARRAY['Hernández','García','Martínez','López','González','Pérez','Rodríguez','Sánchez','Ramírez','Cruz',
           'Flores','Gómez','Morales','Vázquez','Jiménez','Reyes','Díaz','Torres','Gutiérrez','Ruiz'])[1+floor(random()*20)::int],
    60 + floor(random()*36)::int,
    CASE WHEN r2 < 0.25 THEN 'Alta' WHEN r2 < 0.70 THEN 'Media' ELSE 'Baja' END,
    (ARRAY['Requiere apoyo con movilidad.','Necesita compañía durante el día.','Apoyo para aseo y alimentación.',
           'Seguimiento de medicamentos.','Requiere supervisión por la tarde.','Apoyo en traslados a consulta.'])[1+floor(random()*6)::int],
    p.status,
    p.lat + (random() - 0.5) * 0.045,
    p.lng + (random() - 0.5) * 0.045,
    TRUE,
    CASE WHEN p.status = 'Pendiente' THEN NULL ELSE
      (SELECT c.id FROM caregivers c WHERE cm_norm(c.alcaldia) = cm_norm(p.alcaldia)
        ORDER BY random() LIMIT 1) END,
    NOW() - (random() * CASE WHEN p.status = 'Pendiente' THEN 12 ELSE 28 END) * INTERVAL '1 day',
    'demo_pacientes_v1'
  FROM (SELECT t.*, random() AS r, random() AS r2, gs
        FROM _plan t, LATERAL generate_series(1, t.n) gs) p;

  -- La línea base del modelo absorbe lo insertado: el semáforo NO cambia por esta carga.
  UPDATE demanda_alcaldia d SET solicitudes_base = d.solicitudes_base + x.n
  FROM (SELECT cm_norm(alcaldia) AS k, COUNT(*) AS n FROM requests
        WHERE origen = 'demo_pacientes_v1' GROUP BY 1) x
  WHERE cm_norm(d.alcaldia) = x.k;

  RAISE NOTICE 'Solicitudes antes: %, ahora: %', n_antes, (SELECT COUNT(*) FROM requests);
END $$;

DROP FUNCTION cm_norm(text);
COMMIT;
