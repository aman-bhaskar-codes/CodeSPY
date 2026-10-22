# ruff: noqa: E501
"""Scoped immutable scan publication and terminal consistency guards."""

from alembic import op

revision = "0013_scan_authority"
down_revision = "0012_scan_storage"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

-- Only contract JSON (strings, integer numbers, booleans, null, arrays, objects).
-- Timestamp normalization occurs before JSON storage, as in codespy-json-v1.
CREATE FUNCTION public.codespy_scan_json(v jsonb) RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT SECURITY INVOKER AS $$
DECLARE answer text;
BEGIN
 CASE jsonb_typeof(v)
 WHEN 'object' THEN
  SELECT '{'||COALESCE(string_agg(to_jsonb(key)::text||':'||public.codespy_scan_json(value),',' ORDER BY key COLLATE "C"),'')||'}' INTO answer FROM jsonb_each(v);
 WHEN 'array' THEN
  SELECT '['||COALESCE(string_agg(public.codespy_scan_json(value),',' ORDER BY ordinality),'')||']' INTO answer FROM jsonb_array_elements(v) WITH ORDINALITY;
 WHEN 'number' THEN
  IF v::text !~ '^-?(0|[1-9][0-9]*)$' THEN RAISE EXCEPTION 'noninteger scan contract number' USING ERRCODE='23514'; END IF;
  answer:=v::text;
 ELSE answer:=v::text;
 END CASE;
 RETURN answer;
END $$;
CREATE FUNCTION public.codespy_scan_digest(v jsonb) RETURNS text LANGUAGE sql IMMUTABLE STRICT SECURITY INVOKER AS $$
 SELECT encode(sha256(convert_to(public.codespy_scan_json(v),'UTF8')),'hex')
$$;
REVOKE ALL ON FUNCTION public.codespy_scan_json(jsonb),public.codespy_scan_digest(jsonb) FROM PUBLIC;

CREATE FUNCTION public.codespy_scan_spec_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM public.source_snapshots s JOIN public.memberships m ON m.org_id=s.org_id
 JOIN public.repository_grants g ON g.org_id=m.org_id AND g.principal_id=m.principal_id AND g.repository_id=s.repository_id
 WHERE s.org_id=NEW.org_id AND s.id=NEW.snapshot_id AND s.repository_id=NEW.repository_id AND s.state='sealed'
 AND m.principal_id=NEW.principal_id AND m.role IN ('owner','admin','member') AND g.permission='write' AND g.revoked_at IS NULL)
 THEN RAISE EXCEPTION 'scan specification authority unavailable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.codespy_scan_attempt_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; r public.analysis_runs%ROWTYPE; cfg jsonb; common jsonb; expected jsonb;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
 SELECT * INTO r FROM public.analysis_runs WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id AND id=NEW.run_id FOR UPDATE;
 IF j.operation IS DISTINCT FROM 'scan_snapshot' OR j.scan_spec_id IS DISTINCT FROM NEW.spec_id OR j.repository_id IS DISTINCT FROM NEW.repository_id
 OR j.snapshot_id IS DISTINCT FROM NEW.snapshot_id OR j.config_hash IS DISTINCT FROM NEW.config_hash OR j.current_attempt_id IS DISTINCT FROM NEW.attempt_id
 OR j.fence IS DISTINCT FROM NEW.fence OR jid IS DISTINCT FROM NEW.job_id OR r.status IS DISTINCT FROM 'running'
 OR r.worker_job_id IS DISTINCT FROM jid OR r.worker_attempt_id IS DISTINCT FROM NEW.attempt_id OR r.worker_fence IS DISTINCT FROM NEW.fence
 OR (NEW.publication_spec_json#>>'{scope,org_id}') IS DISTINCT FROM NEW.org_id::text
 OR (NEW.publication_spec_json#>>'{scope,snapshot_id}') IS DISTINCT FROM NEW.snapshot_id::text
 OR (NEW.publication_spec_json#>>'{scope,run_id}') IS DISTINCT FROM NEW.run_id::text
 OR (NEW.publication_spec_json->>'config_hash') IS DISTINCT FROM NEW.config_hash
 OR (NEW.publication_spec_json->>'repository_id') IS DISTINCT FROM NEW.repository_id::text
 OR (NEW.publication_spec_json->>'captured_at')::timestamptz IS DISTINCT FROM NEW.captured_at
 THEN RAISE EXCEPTION 'foreign scan attempt' USING ERRCODE='23514'; END IF;

 SELECT config_json INTO cfg FROM public.scan_specs WHERE org_id=NEW.org_id AND id=NEW.spec_id;
 common:=jsonb_build_object('schema_version',1,'scope',jsonb_build_object('org_id',NEW.org_id::text,'snapshot_id',NEW.snapshot_id::text,'run_id',NEW.run_id::text),'repository_id',NEW.repository_id::text,'config_hash',NEW.config_hash);
 IF NEW.publication_spec_json->>'runtime_digest' IS DISTINCT FROM cfg->>'runtime_digest'
 OR NEW.publication_spec_json->>'catalog_digest' IS DISTINCT FROM cfg->>'catalog_digest'
 OR jsonb_typeof(NEW.publication_spec_json->'native_requests') IS DISTINCT FROM 'array'
 OR EXISTS(SELECT 1 FROM (
   (SELECT common||jsonb_build_object('profile',n->'profile','files',n->'files') FROM jsonb_array_elements(cfg->'native_selections') n
    EXCEPT ALL SELECT value FROM jsonb_array_elements(NEW.publication_spec_json->'native_requests'))
   UNION ALL
   (SELECT value FROM jsonb_array_elements(NEW.publication_spec_json->'native_requests')
    EXCEPT ALL SELECT common||jsonb_build_object('profile',n->'profile','files',n->'files') FROM jsonb_array_elements(cfg->'native_selections') n)
 ) differences)
 THEN RAISE EXCEPTION 'scan attempt configuration differs' USING ERRCODE='23514'; END IF;
 expected:='null'::jsonb;
 IF cfg->'dependency_selection' IS DISTINCT FROM 'null'::jsonb THEN
  expected:=common||jsonb_build_object('runtime_digest',cfg->'runtime_digest','bundle_digest',cfg#>'{dependency_selection,bundle_digest}',
   'parser_profile',cfg#>'{dependency_selection,parser_profile}','files',cfg#>'{dependency_selection,files}',
   'evaluated_at',NEW.publication_spec_json->'captured_at');
 END IF;
 IF NEW.publication_spec_json->'dependency_request' IS DISTINCT FROM expected
 THEN RAISE EXCEPTION 'scan dependency attempt configuration differs' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.codespy_scan_result_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; r public.analysis_runs%ROWTYPE; a public.scan_attempt_specs%ROWTYPE; f uuid; p jsonb;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
 SELECT * INTO r FROM public.analysis_runs WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id AND id=NEW.run_id FOR UPDATE;
 SELECT * INTO a FROM public.scan_attempt_specs WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id AND run_id=NEW.run_id;
 IF j.operation IS DISTINCT FROM 'scan_snapshot' OR r.status IS DISTINCT FROM 'running' OR a.run_id IS NULL
 OR r.worker_job_id IS DISTINCT FROM jid OR r.worker_attempt_id IS DISTINCT FROM j.current_attempt_id OR r.worker_fence IS DISTINCT FROM j.fence
 OR a.job_id IS DISTINCT FROM jid OR a.attempt_id IS DISTINCT FROM j.current_attempt_id OR a.fence IS DISTINCT FROM j.fence
 OR a.spec_id IS DISTINCT FROM j.scan_spec_id OR a.config_hash IS DISTINCT FROM j.config_hash
 THEN RAISE EXCEPTION 'foreign scan result' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='scan_reports' THEN
  IF NEW.repository_id IS DISTINCT FROM j.repository_id OR NEW.spec_id IS DISTINCT FROM j.scan_spec_id
  OR NEW.config_hash IS DISTINCT FROM j.config_hash OR NEW.draft_json->'spec' IS DISTINCT FROM a.publication_spec_json
  OR NEW.draft_json->>'coverage' IS DISTINCT FROM NEW.coverage
  OR (NEW.draft_json->>'omitted_findings')::bigint IS DISTINCT FROM NEW.omitted_findings
  THEN RAISE EXCEPTION 'scan report specification differs' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME IN ('scan_observations','finding_occurrences') THEN
  p:=NEW.payload_json; f:=NEW.file_id;
  IF NOT EXISTS(SELECT 1 FROM public.source_files sf WHERE sf.org_id=NEW.org_id AND sf.snapshot_id=NEW.snapshot_id AND sf.id=f)
  THEN RAISE EXCEPTION 'foreign scan source' USING ERRCODE='23514'; END IF;
  IF TG_TABLE_NAME='scan_observations' AND p->>'file_id' IS DISTINCT FROM f::text
  THEN RAISE EXCEPTION 'observation file differs' USING ERRCODE='23514'; END IF;
  IF TG_TABLE_NAME='finding_occurrences' THEN
   IF p->>'id' IS DISTINCT FROM NEW.id::text OR p#>>'{entity,file_id}' IS DISTINCT FROM f::text
     OR p->>'category' IS DISTINCT FROM NEW.category OR p->>'rule_id' IS DISTINCT FROM NEW.rule_id
   THEN RAISE EXCEPTION 'occurrence identity differs' USING ERRCODE='23514'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_scan_complete_set(oid uuid,sid uuid,rid uuid) RETURNS void LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE d jsonb; rep public.scan_reports%ROWTYPE; a public.scan_attempt_specs%ROWTYPE; r public.analysis_runs%ROWTYPE; item jsonb; support_count bigint; native_item jsonb; req jsonb; res jsonb; candidate_count bigint:=0; complete boolean:=true; observed boolean:=false; expected_coverage text; request_digests jsonb:='{}'; result_digests jsonb:='{}';
BEGIN
 SELECT * INTO rep FROM public.scan_reports WHERE org_id=oid AND snapshot_id=sid AND run_id=rid;
 SELECT * INTO a FROM public.scan_attempt_specs WHERE org_id=oid AND snapshot_id=sid AND run_id=rid;
 SELECT * INTO r FROM public.analysis_runs WHERE org_id=oid AND snapshot_id=sid AND id=rid;
 IF rep.run_id IS NULL OR a.run_id IS NULL OR r.id IS NULL OR rep.draft_json->'spec' IS DISTINCT FROM a.publication_spec_json
 OR r.config_hash IS DISTINCT FROM a.config_hash OR r.analyzer_name<>'codespy-findings' OR r.analyzer_version<>'1'
 OR r.worker_job_id IS DISTINCT FROM a.job_id OR r.worker_attempt_id IS DISTINCT FROM a.attempt_id OR r.worker_fence IS DISTINCT FROM a.fence
 THEN RAISE EXCEPTION 'incomplete scan publication' USING ERRCODE='23514'; END IF;
 d:=rep.draft_json;
 IF jsonb_typeof(d->'native_results') IS DISTINCT FROM 'array' OR jsonb_typeof(d->'findings') IS DISTINCT FROM 'array'
 OR jsonb_typeof(d->'evidence') IS DISTINCT FROM 'array' OR jsonb_typeof(d->'decisions') IS DISTINCT FROM 'array'
 OR jsonb_array_length(d->'findings')>1000 OR jsonb_array_length(d->'evidence')>1000 OR jsonb_array_length(d->'decisions')>1000
 THEN RAISE EXCEPTION 'invalid scan publication shape' USING ERRCODE='23514'; END IF;

 IF jsonb_array_length(d->'native_results')<>jsonb_array_length(a.publication_spec_json->'native_requests')
 OR (SELECT count(DISTINCT x->>'tool') FROM jsonb_array_elements(d->'native_results') x)<>jsonb_array_length(d->'native_results')
 OR (d->'dependency_result' IS DISTINCT FROM 'null'::jsonb)<>(a.publication_spec_json->'dependency_request' IS DISTINCT FROM 'null'::jsonb)
 THEN RAISE EXCEPTION 'configured scan operation set differs' USING ERRCODE='23514'; END IF;
 FOR native_item IN SELECT value FROM jsonb_array_elements(d->'native_results') LOOP
  SELECT value INTO req FROM jsonb_array_elements(a.publication_spec_json->'native_requests') WHERE value#>>'{profile,tool}'=native_item->>'tool';
  res:=native_item->'result';
  IF req IS NULL OR res->>'request_digest' IS DISTINCT FROM public.codespy_scan_digest(req)
   OR res->>'status' NOT IN ('completed','partial')
   OR jsonb_typeof(res->'observations') IS DISTINCT FROM 'array'
   OR jsonb_typeof(res->'processed_file_ids') IS DISTINCT FROM 'array'
   OR jsonb_typeof(res->'omitted_file_ids') IS DISTINCT FROM 'array'
   OR EXISTS(SELECT 1 FROM (
    (SELECT value->'file_id' FROM jsonb_array_elements(req->'files') EXCEPT ALL
      (SELECT value FROM jsonb_array_elements(res->'processed_file_ids') UNION ALL SELECT value FROM jsonb_array_elements(res->'omitted_file_ids')))
    UNION ALL
    ((SELECT value FROM jsonb_array_elements(res->'processed_file_ids') UNION ALL SELECT value FROM jsonb_array_elements(res->'omitted_file_ids')) EXCEPT ALL
      SELECT value->'file_id' FROM jsonb_array_elements(req->'files'))
   ) differences)
   OR (res->>'status'='completed' AND jsonb_array_length(res->'omitted_file_ids')<>0)
   OR EXISTS(SELECT 1 FROM jsonb_array_elements(res->'observations') o WHERE NOT EXISTS(
    SELECT 1 FROM jsonb_array_elements(req->'files') f WHERE f->>'file_id'=o->>'file_id' AND f->>'sha256'=o->>'sha256'
     AND res->'processed_file_ids' @> jsonb_build_array(f->'file_id') AND req#>'{profile,rule_ids}' @> jsonb_build_array(o->'rule_id')
     AND (o#>>'{range,start_byte}')::bigint>=0 AND (o#>>'{range,end_byte}')::bigint>(o#>>'{range,start_byte}')::bigint
     AND (o#>>'{range,end_byte}')::bigint<=(f->>'size_bytes')::bigint))
  THEN RAISE EXCEPTION 'native scan result binding differs' USING ERRCODE='23514'; END IF;
  request_digests:=request_digests||jsonb_build_object(native_item->>'tool',res->>'request_digest');
  result_digests:=result_digests||jsonb_build_object(native_item->>'tool',public.codespy_scan_digest(res));
  candidate_count:=candidate_count+jsonb_array_length(res->'observations');
  complete:=complete AND res->>'status'='completed';
  observed:=observed OR jsonb_array_length(res->'processed_file_ids')>0;
 END LOOP;
 IF d->'dependency_result' IS DISTINCT FROM 'null'::jsonb THEN
  req:=a.publication_spec_json->'dependency_request'; res:=d->'dependency_result';
  IF res->>'request_digest' IS DISTINCT FROM public.codespy_scan_digest(req)
   OR jsonb_typeof(res->'files') IS DISTINCT FROM 'array' OR jsonb_typeof(res->'packages') IS DISTINCT FROM 'array'
   OR jsonb_typeof(res->'unresolved') IS DISTINCT FROM 'array'
   OR EXISTS(SELECT 1 FROM (
    (SELECT value->'file_id' FROM jsonb_array_elements(req->'files') EXCEPT ALL SELECT value->'file_id' FROM jsonb_array_elements(res->'files'))
    UNION ALL (SELECT value->'file_id' FROM jsonb_array_elements(res->'files') EXCEPT ALL SELECT value->'file_id' FROM jsonb_array_elements(req->'files'))
   ) differences)
   OR EXISTS(SELECT 1 FROM (SELECT value FROM jsonb_array_elements(res->'packages') UNION ALL SELECT value FROM jsonb_array_elements(res->'unresolved')) p
    WHERE NOT EXISTS(SELECT 1 FROM jsonb_array_elements(res->'files') f WHERE f->>'file_id'=p.value->>'file_id' AND f->>'status'='parsed'))
  THEN RAISE EXCEPTION 'dependency scan result binding differs' USING ERRCODE='23514'; END IF;
  request_digests:=request_digests||jsonb_build_object('dependencies',res->>'request_digest');
  result_digests:=result_digests||jsonb_build_object('dependencies',public.codespy_scan_digest(res));
  expected_coverage:=CASE WHEN jsonb_array_length(req->'files')>0 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(res->'files') f WHERE f->>'status'<>'parsed')
    AND jsonb_array_length(res->'unresolved')=0 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(res->'packages') p WHERE p#>>'{assessment,status}' NOT IN ('matched','empty')) THEN 'complete'
   WHEN EXISTS(SELECT 1 FROM jsonb_array_elements(res->'files') f WHERE f->>'status'='parsed') THEN 'partial' ELSE 'unknown' END;
  IF res->>'coverage' IS DISTINCT FROM expected_coverage THEN RAISE EXCEPTION 'dependency scan coverage differs' USING ERRCODE='23514'; END IF;
  SELECT candidate_count+COALESCE(sum(jsonb_array_length(p#>'{assessment,advisories}')),0) INTO candidate_count FROM jsonb_array_elements(res->'packages') p;
  complete:=complete AND res->>'coverage'='complete';
  observed:=observed OR EXISTS(SELECT 1 FROM jsonb_array_elements(res->'files') f WHERE f->>'status'='parsed');
 END IF;
 IF jsonb_array_length(d->'findings')<>LEAST(candidate_count,1000) OR rep.omitted_findings<>GREATEST(candidate_count-1000,0)
 THEN RAISE EXCEPTION 'scan candidate accounting differs' USING ERRCODE='23514'; END IF;
 expected_coverage:=CASE WHEN complete AND rep.omitted_findings=0 THEN 'complete' WHEN complete OR observed THEN 'partial' ELSE 'unknown' END;
 IF rep.coverage IS DISTINCT FROM expected_coverage THEN RAISE EXCEPTION 'scan aggregate coverage differs' USING ERRCODE='23514'; END IF;
 IF (SELECT count(DISTINCT x->>'id') FROM jsonb_array_elements(d->'evidence') x)<>jsonb_array_length(d->'evidence')
 OR EXISTS(SELECT 1 FROM (
  SELECT value FROM jsonb_array_elements(d->'findings') UNION ALL
  SELECT value FROM jsonb_array_elements(d->'evidence') UNION ALL
  SELECT value FROM jsonb_array_elements(d->'decisions')
 ) scoped WHERE value->'scope' IS DISTINCT FROM jsonb_build_object('org_id',oid::text,'snapshot_id',sid::text,'run_id',rid::text))
 THEN RAISE EXCEPTION 'scan evidence identity or scope differs' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.cpg_nodes WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)
 OR EXISTS(SELECT 1 FROM public.cpg_edges WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)
 OR EXISTS(SELECT 1 FROM public.evidence_derivations WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)
 THEN RAISE EXCEPTION 'scan publication contains graph records' USING ERRCODE='23514'; END IF;
 IF EXISTS(
 WITH expected AS (
  SELECT n->>'tool' AS origin,'native'::text AS kind,o AS payload FROM jsonb_array_elements(d->'native_results') n
    CROSS JOIN LATERAL jsonb_array_elements(n#>'{result,observations}') o
  UNION ALL SELECT 'dependencies','dependency_instance',p FROM jsonb_array_elements(COALESCE(d#>'{dependency_result,packages}','[]'::jsonb)) p
  UNION ALL SELECT 'dependencies','unresolved_dependency',p FROM jsonb_array_elements(COALESCE(d#>'{dependency_result,unresolved}','[]'::jsonb)) p
 ), actual AS (SELECT origin,kind,payload_json AS payload FROM public.scan_observations WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)
 SELECT 1 FROM ((SELECT * FROM expected EXCEPT ALL SELECT * FROM actual) UNION ALL
                (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)) differences
 ) OR (SELECT count(*) FROM public.scan_observations WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)>7000
 THEN RAISE EXCEPTION 'scan observation set differs' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM (
  (SELECT value FROM jsonb_array_elements(d->'findings') EXCEPT ALL SELECT payload_json FROM public.finding_occurrences WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)
  UNION ALL (SELECT payload_json FROM public.finding_occurrences WHERE org_id=oid AND snapshot_id=sid AND run_id=rid EXCEPT ALL SELECT value FROM jsonb_array_elements(d->'findings'))
 ) differences) THEN RAISE EXCEPTION 'finding set differs' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM (
  (SELECT value FROM jsonb_array_elements(d->'decisions') EXCEPT ALL SELECT payload_json FROM public.verification_decisions WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)
  UNION ALL (SELECT payload_json FROM public.verification_decisions WHERE org_id=oid AND snapshot_id=sid AND run_id=rid EXCEPT ALL SELECT value FROM jsonb_array_elements(d->'decisions'))
 ) differences) THEN RAISE EXCEPTION 'verification set differs' USING ERRCODE='23514'; END IF;
 support_count:=0;
 FOR item IN SELECT value FROM jsonb_array_elements(d->'findings') LOOP
  IF NOT EXISTS(SELECT 1 FROM public.finding_occurrences f JOIN public.scan_observations o ON o.org_id=f.org_id AND o.snapshot_id=f.snapshot_id AND o.run_id=f.run_id AND o.observation_key=f.observation_key
    WHERE f.org_id=oid AND f.snapshot_id=sid AND f.run_id=rid AND f.id=(item->>'id')::uuid AND f.file_id=o.file_id
    AND ((item#>>'{entity,kind}'='source' AND o.kind='native' AND o.origin=item#>>'{entity,tool}'
      AND o.payload_json->>'rule_id'=item->>'rule_id' AND o.payload_json->>'sha256'=item#>>'{entity,sha256}' AND o.payload_json->'range'=item#>'{entity,range}')
    OR (item#>>'{entity,kind}'='dependency' AND o.kind='dependency_instance' AND o.payload_json->>'location'=item#>>'{entity,location}'
      AND o.payload_json->'query'=item#>'{entity,query}' AND o.payload_json#>>'{assessment,status}'='matched'
      AND o.payload_json#>'{assessment,advisories}' @> jsonb_build_array(jsonb_build_array(item#>>'{entity,advisory_id}',item#>>'{entity,record_digest}')))))
  THEN RAISE EXCEPTION 'finding affected observation differs' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.source_files sf WHERE sf.org_id=oid AND sf.snapshot_id=sid AND sf.id=(item#>>'{entity,file_id}')::uuid
    AND sf.sha256=item#>>'{entity,sha256}') THEN RAISE EXCEPTION 'finding source hash differs' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.verification_decisions v WHERE v.org_id=oid AND v.snapshot_id=sid AND v.run_id=rid AND v.finding_id=(item->>'id')::uuid
    AND v.payload_json->>'finding_id'=item->>'id' AND v.payload_json->'support'=item->'support' AND v.disposition=v.payload_json->>'disposition'
    AND v.payload_json->'scope'=item->'scope' AND v.payload_json->'obligations'=CASE item->>'category'
      WHEN 'source_pattern' THEN '["authenticated-source","qualified-predicate"]'::jsonb
      WHEN 'secret_format' THEN '["authenticated-source","selected-format"]'::jsonb
      ELSE '["exact-lock-query","fresh-complete-advisory"]'::jsonb END)
  THEN RAISE EXCEPTION 'verification obligations differ' USING ERRCODE='23514'; END IF;
  support_count:=support_count+jsonb_array_length(item->'support');
  IF jsonb_array_length(item->'support')=0 OR EXISTS(SELECT 1 FROM jsonb_array_elements(item->'support') x WHERE NOT EXISTS(
    SELECT 1 FROM public.finding_support fs JOIN public.evidence_records e ON e.org_id=fs.org_id AND e.snapshot_id=fs.snapshot_id AND e.run_id=fs.run_id AND e.id=fs.evidence_id
    WHERE fs.org_id=oid AND fs.snapshot_id=sid AND fs.run_id=rid AND fs.finding_id=(item->>'id')::uuid AND fs.evidence_id=(x->>'evidence_id')::uuid
    AND x->'scope'=item->'scope' AND e.file_id=(item#>>'{entity,file_id}')::uuid AND e.payload->>'observation_digest'=item->>'observation_digest'
    AND e.payload->>'request_digest'=request_digests->>CASE WHEN item#>>'{entity,kind}'='dependency' THEN 'dependencies' ELSE item#>>'{entity,tool}' END
    AND e.payload->>'result_digest'=result_digests->>CASE WHEN item#>>'{entity,kind}'='dependency' THEN 'dependencies' ELSE item#>>'{entity,tool}' END
    AND e.payload->>'policy_id'=item->>'policy_id' AND e.payload->>'policy_version'=item->>'policy_version'))
  THEN RAISE EXCEPTION 'finding support differs' USING ERRCODE='23514'; END IF;
 END LOOP;
 IF (SELECT count(*) FROM public.finding_support WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)<>support_count
 OR (SELECT count(*) FROM public.evidence_records WHERE org_id=oid AND snapshot_id=sid AND run_id=rid)<>jsonb_array_length(d->'evidence')
 THEN RAISE EXCEPTION 'scan evidence set differs' USING ERRCODE='23514'; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(d->'evidence') LOOP
  IF NOT EXISTS(SELECT 1 FROM public.evidence_records e WHERE e.org_id=oid AND e.snapshot_id=sid AND e.run_id=rid AND e.id=(item->>'id')::uuid
    AND e.file_id IS NOT DISTINCT FROM (item->>'file_id')::uuid AND e.schema_version=(item->>'schema_version')::int
    AND e.provider_name=item->>'provider_name' AND e.provider_version=item->>'provider_version' AND e.operation=item->>'operation'
    AND e.observation_mode=item->>'observation_mode' AND e.basis=item->>'basis' AND e.coverage_status=item->>'coverage_status'
    AND e.payload=item->'payload' AND e.limitations=item->'limitations' AND e.captured_at=(item->>'captured_at')::timestamptz
    AND e.start_byte IS NOT DISTINCT FROM (item#>>'{range,start_byte}')::bigint AND e.end_byte IS NOT DISTINCT FROM (item#>>'{range,end_byte}')::bigint
    AND e.start_line IS NOT DISTINCT FROM (item#>>'{range,start_line}')::int AND e.end_line IS NOT DISTINCT FROM (item#>>'{range,end_line}')::int
    AND e.start_col IS NOT DISTINCT FROM (item#>>'{range,start_col}')::int AND e.end_col IS NOT DISTINCT FROM (item#>>'{range,end_col}')::int)
  THEN RAISE EXCEPTION 'scan evidence content differs' USING ERRCODE='23514'; END IF;
 END LOOP;
END $$;

CREATE FUNCTION public.codespy_scan_job_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF TG_OP='UPDATE' AND (OLD.operation='scan_snapshot' OR NEW.operation='scan_snapshot') AND
 ROW(NEW.org_id,NEW.id,NEW.repository_id,NEW.principal_id,NEW.snapshot_id,NEW.scan_spec_id,NEW.config_hash,NEW.operation,NEW.request_key,NEW.request_hash,NEW.submission_id,NEW.parent_job_id,NEW.retry_of_job_id)
 IS DISTINCT FROM ROW(OLD.org_id,OLD.id,OLD.repository_id,OLD.principal_id,OLD.snapshot_id,OLD.scan_spec_id,OLD.config_hash,OLD.operation,OLD.request_key,OLD.request_hash,OLD.submission_id,OLD.parent_job_id,OLD.retry_of_job_id)
 THEN RAISE EXCEPTION 'immutable scan job binding' USING ERRCODE='23514'; END IF;
 IF NEW.operation='scan_snapshot' THEN
  IF NOT EXISTS(SELECT 1 FROM public.scan_specs s WHERE s.org_id=NEW.org_id AND s.id=NEW.scan_spec_id AND s.principal_id=NEW.principal_id)
  THEN RAISE EXCEPTION 'scan principal differs' USING ERRCODE='23514'; END IF;
  IF TG_OP='UPDATE' AND OLD.state NOT IN ('succeeded','partial','failed','cancelled') AND NEW.state IN ('succeeded','partial') THEN
   PERFORM public.codespy_scan_complete_set(NEW.org_id,NEW.snapshot_id,NEW.result_run_id);
   IF NOT EXISTS(SELECT 1 FROM public.analysis_runs r JOIN public.scan_reports rep ON rep.org_id=r.org_id AND rep.snapshot_id=r.snapshot_id AND rep.run_id=r.id
      WHERE r.org_id=NEW.org_id AND r.snapshot_id=NEW.snapshot_id AND r.id=NEW.result_run_id AND r.worker_job_id=NEW.id
      AND r.worker_attempt_id=NEW.current_attempt_id AND r.worker_fence=NEW.fence AND r.result_hash=rep.batch_hash
      AND r.status=CASE WHEN NEW.state='succeeded' THEN 'completed' ELSE 'partial' END
      AND (rep.coverage='complete')=(NEW.state='succeeded'))
   THEN RAISE EXCEPTION 'scan terminal association differs' USING ERRCODE='23514'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.codespy_scan_run_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE j public.jobs%ROWTYPE;
BEGIN
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=NEW.worker_job_id;
 IF j.operation='scan_snapshot' OR NEW.analyzer_name='codespy-findings' THEN
  IF j.operation IS DISTINCT FROM 'scan_snapshot' OR NEW.analyzer_name<>'codespy-findings' OR NEW.analyzer_version<>'1'
  OR NOT EXISTS(SELECT 1 FROM public.scan_specs s WHERE s.org_id=j.org_id AND s.id=j.scan_spec_id AND s.config_hash=NEW.config_hash AND s.config_json=NEW.config)
  THEN RAISE EXCEPTION 'scan run binding differs' USING ERRCODE='23514'; END IF;
  IF NEW.status IN ('completed','partial') THEN PERFORM public.codespy_scan_complete_set(NEW.org_id,NEW.snapshot_id,NEW.id); END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.codespy_scan_committed_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE r public.analysis_runs%ROWTYPE; a public.scan_attempt_specs%ROWTYPE; j public.jobs%ROWTYPE; attempt public.job_attempts%ROWTYPE; rid uuid;
BEGIN
 IF TG_TABLE_NAME='scan_reports' THEN rid:=NEW.run_id; ELSE rid:=NEW.id; END IF;
 SELECT * INTO r FROM public.analysis_runs WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id AND id=rid;
 IF r.analyzer_name IS DISTINCT FROM 'codespy-findings' THEN RETURN NULL; END IF;
 SELECT * INTO a FROM public.scan_attempt_specs WHERE org_id=r.org_id AND snapshot_id=r.snapshot_id AND run_id=r.id;
 SELECT * INTO j FROM public.jobs WHERE org_id=r.org_id AND id=r.worker_job_id;
 SELECT * INTO attempt FROM public.job_attempts WHERE org_id=r.org_id AND job_id=r.worker_job_id AND id=r.worker_attempt_id;
 IF a.run_id IS NULL OR a.job_id IS DISTINCT FROM j.id OR a.attempt_id IS DISTINCT FROM r.worker_attempt_id OR a.fence IS DISTINCT FROM r.worker_fence
 THEN RAISE EXCEPTION 'scan attempt specification missing' USING ERRCODE='23514'; END IF;
 IF r.status IN ('completed','partial') THEN
  IF j.state NOT IN ('succeeded','partial') OR j.result_run_id IS DISTINCT FROM r.id OR j.current_attempt_id IS DISTINCT FROM attempt.id
  OR j.fence IS DISTINCT FROM r.worker_fence OR attempt.state IS DISTINCT FROM 'succeeded' OR j.publication_checked_at IS NULL
  THEN RAISE EXCEPTION 'scan terminal publication incomplete' USING ERRCODE='23514'; END IF;
  PERFORM public.codespy_scan_complete_set(r.org_id,r.snapshot_id,r.id);
 ELSIF EXISTS(SELECT 1 FROM public.scan_reports WHERE org_id=r.org_id AND snapshot_id=r.snapshot_id AND run_id=r.id) THEN
  RAISE EXCEPTION 'scan report requires terminal publication' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;

CREATE TRIGGER guard_scan_spec BEFORE INSERT ON public.scan_specs FOR EACH ROW EXECUTE FUNCTION public.codespy_scan_spec_guard();
CREATE TRIGGER guard_scan_attempt BEFORE INSERT ON public.scan_attempt_specs FOR EACH ROW EXECUTE FUNCTION public.codespy_scan_attempt_guard();
CREATE TRIGGER guard_scan_job BEFORE INSERT OR UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.codespy_scan_job_guard();
CREATE TRIGGER guard_scan_run BEFORE INSERT OR UPDATE ON public.analysis_runs FOR EACH ROW EXECUTE FUNCTION public.codespy_scan_run_guard();
CREATE CONSTRAINT TRIGGER guard_scan_run_committed AFTER INSERT OR UPDATE ON public.analysis_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.codespy_scan_committed_guard();
CREATE CONSTRAINT TRIGGER guard_scan_report_committed AFTER INSERT ON public.scan_reports DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.codespy_scan_committed_guard();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['scan_specs','scan_attempt_specs','scan_reports','scan_observations','finding_occurrences','finding_support','verification_decisions'] LOOP
  EXECUTE format('CREATE TRIGGER guard_scan_immutable BEFORE UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation()',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['scan_reports','scan_observations','finding_occurrences','finding_support','verification_decisions'] LOOP
  EXECUTE format('CREATE TRIGGER guard_scan_result BEFORE INSERT ON public.%I FOR EACH ROW EXECUTE FUNCTION public.codespy_scan_result_guard()',t);
 END LOOP;
END $$;
REVOKE ALL ON FUNCTION public.codespy_scan_spec_guard(),public.codespy_scan_attempt_guard(),public.codespy_scan_result_guard(),public.codespy_scan_complete_set(uuid,uuid,uuid),public.codespy_scan_job_guard(),public.codespy_scan_run_guard(),public.codespy_scan_committed_guard() FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(r"""
    DO $$ DECLARE t text; BEGIN
      IF EXISTS(SELECT 1 FROM public.scan_specs) OR EXISTS(SELECT 1 FROM public.jobs WHERE operation='scan_snapshot') THEN RAISE EXCEPTION 'scan history must be preserved'; END IF;
      FOREACH t IN ARRAY ARRAY['scan_reports','scan_observations','finding_occurrences','finding_support','verification_decisions'] LOOP
        EXECUTE format('DROP TRIGGER guard_scan_result ON public.%I',t);
      END LOOP;
      FOREACH t IN ARRAY ARRAY['scan_specs','scan_attempt_specs','scan_reports','scan_observations','finding_occurrences','finding_support','verification_decisions'] LOOP
        EXECUTE format('DROP TRIGGER guard_scan_immutable ON public.%I',t);
      END LOOP;
    END $$;
    DROP TRIGGER guard_scan_report_committed ON public.scan_reports;
    DROP TRIGGER guard_scan_run_committed ON public.analysis_runs;
    DROP TRIGGER guard_scan_run ON public.analysis_runs;
    DROP TRIGGER guard_scan_job ON public.jobs;
    DROP TRIGGER guard_scan_attempt ON public.scan_attempt_specs;
    DROP TRIGGER guard_scan_spec ON public.scan_specs;
    DROP FUNCTION public.codespy_scan_digest(jsonb),public.codespy_scan_json(jsonb);
    DROP FUNCTION public.codespy_scan_committed_guard(),public.codespy_scan_run_guard(),public.codespy_scan_job_guard(),public.codespy_scan_complete_set(uuid,uuid,uuid),public.codespy_scan_result_guard(),public.codespy_scan_attempt_guard(),public.codespy_scan_spec_guard();
    """)
