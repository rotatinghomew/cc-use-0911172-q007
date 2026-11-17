from __future__ import annotations
import datetime as dt
import hashlib,json
from collections import Counter
class EvidenceRegistry:
 # Submission-event metadata (upload time, batch, review annotations) describes how a
 # material was submitted, not what it is; it never takes part in material identity.
 SUBMISSION_FIELDS=frozenset({'received_at','received_by','review_note','uploaded_at','submitted_at','upload_batch','batch','batch_id','submission_id','upload_id','digest'})
 def canonical(self,row):return json.dumps({k:row[k] for k in sorted(row) if k not in self.SUBMISSION_FIELDS},ensure_ascii=False,separators=(',',':'))
 def digest(self,row):return hashlib.sha256(self.canonical(row).encode()).hexdigest()
 def active(self,evidence,today):
  result=[]
  for row in evidence:
   if row.get('revoked'):continue
   if row.get('expires') and dt.date.fromisoformat(str(row['expires']))<today:continue
   result.append(dict(row,digest=self.digest(row)))
  return result
 def distinct(self,rows):
  # Collapse repeated submissions of the same material (identical content digest) into
  # one countable unit. Suppressed copies are reported for audit, never deleted.
  seen=set();unique=[];suppressed={}
  for row in rows:
   digest=row.get('digest') or self.digest(row)
   if digest in seen:
    entry=suppressed.setdefault(digest,{'digest':digest,'type':row.get('type'),'copies':0});entry['copies']+=1;continue
   seen.add(digest);unique.append(row)
  return unique,[suppressed[key] for key in sorted(suppressed)]
 def evaluate(self,application,rulebook,today):
  active=self.active(application.get('evidence',()),today);distinct,duplicates=self.distinct(active);counts=Counter(row.get('type') for row in distinct);reasons=[];used=[]
  for rule in rulebook.get('rules',()):
   available=counts[str(rule['type'])];minimum=int(rule.get('minimum',1))
   if available<minimum:reasons.append('rule_unsatisfied:'+str(rule['type']))
   else:used.extend(row['digest'] for row in distinct if row.get('type')==rule['type'])
  return {'accepted':not reasons,'reasons':reasons,'rulebook_version':rulebook.get('version'),'evidence_digests':sorted(set(used)),'active_counts':dict(counts),'submissions':len(active),'duplicates':duplicates}
 def replay(self,application,rulebooks,today):return {str(book['version']):self.evaluate(application,book,today) for book in rulebooks}
 def changed_decisions(self,replay):
  versions=sorted(replay);changes=[]
  for left,right in zip(versions,versions[1:]):
   if replay[left]['accepted']!=replay[right]['accepted'] or replay[left]['reasons']!=replay[right]['reasons']:changes.append({'from':left,'to':right,'accepted_from':replay[left]['accepted'],'accepted_to':replay[right]['accepted']})
  return changes
 def appeal_scope(self,old_evidence,new_evidence):
  old={self.digest(row) for row in old_evidence};new={self.digest(row) for row in new_evidence};return {'added':sorted(new-old),'removed':sorted(old-new),'requires_full_review':bool(old-new)}
