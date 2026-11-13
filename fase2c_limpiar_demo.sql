-- =====================================================================
-- CuidarMéx · Quita lo agregado por fase2a (pacientes demo) y fase2b (cuidadores simulados)
-- Deja la base como estaba antes (el semáforo vuelve a la tabla del proyecto).
-- =====================================================================
BEGIN;

CREATE OR REPLACE FUNCTION cm_norm(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(regexp_replace(translate(lower(trim(t)),'áéíóúü','aeiouu'),' de morelos$',''),'^la ','')
$$;

-- Pacientes demo: restar de la línea base lo que se había absorbido, y borrarlos
UPDATE demanda_alcaldia d SET solicitudes_base = GREATEST(0, d.solicitudes_base - x.n)
FROM (SELECT cm_norm(alcaldia) AS k, COUNT(*) AS n FROM requests
      WHERE origen = 'demo_pacientes_v1' GROUP BY 1) x
WHERE cm_norm(d.alcaldia) = x.k;

DELETE FROM applications   WHERE request_id IN (SELECT id FROM requests WHERE origen = 'demo_pacientes_v1');
DELETE FROM service_ratings WHERE request_id IN (SELECT id FROM requests WHERE origen = 'demo_pacientes_v1');
DELETE FROM requests WHERE origen = 'demo_pacientes_v1';

-- Cuidadores simulados
DELETE FROM applications     WHERE caregiver_id IN (SELECT id FROM caregivers WHERE origen = 'simulacion_v1');
DELETE FROM training_progress WHERE caregiver_id IN (SELECT id FROM caregivers WHERE origen = 'simulacion_v1');
DELETE FROM certifications   WHERE caregiver_id IN (SELECT id FROM caregivers WHERE origen = 'simulacion_v1');
DELETE FROM caregivers WHERE origen = 'simulacion_v1';

DROP FUNCTION cm_norm(text);
COMMIT;
