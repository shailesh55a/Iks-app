import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP_DB = ROOT / 'app' / 'iksphere.db'
if APP_DB.exists(): APP_DB.unlink()

from app import main
from app.main import app, postgres_sql

@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c

def login(client, email='test@example.com', role='learner', name='Test Learner'):
    r = client.post('/api/auth/login', json={'name':name,'email':email,'role':role})
    assert r.status_code == 200, r.text
    return r.json()

def test_health_and_legacy_catalog_removed(client):
    health = client.get('/api/health')
    assert health.status_code == 200
    body = health.json()
    assert body['status'] == 'ok'
    assert body['version'].startswith('5.0')
    assert 'gemini' not in body
    assert client.get('/api/syllabus').status_code == 404

def test_postgres_query_adapter_translates_sqlite_helpers():
    assert postgres_sql('SELECT * FROM users WHERE id=?') == 'SELECT * FROM users WHERE id=%s'
    assert postgres_sql('INSERT OR IGNORE INTO users(id) VALUES (?)') == 'INSERT INTO users(id) VALUES (%s) ON CONFLICT DO NOTHING'
    assert postgres_sql('ALTER TABLE users ADD COLUMN provider TEXT') == 'ALTER TABLE users ADD COLUMN IF NOT EXISTS provider TEXT'
    assert postgres_sql('PRAGMA foreign_keys=ON') is None

def test_hosted_configuration_requires_firebase_and_disables_demo_role_selection(client, monkeypatch):
    monkeypatch.setattr(main, 'FIREBASE_PROJECT_ID', '')
    monkeypatch.setattr(main, 'SUPABASE_DB_URL', 'postgresql://hosted.example/iksphere')
    blocked_login = client.post('/api/auth/login', json={'name':'Untrusted Teacher','email':'fake@example.com','role':'teacher'})
    assert blocked_login.status_code == 403
    blocked_data = client.get('/api/courses', params={'user_id':'demo-student'})
    assert blocked_data.status_code == 401
    blocked_token = client.post('/api/auth/firebase', json={'id_token':'unverified-token-with-enough-length','name':'Unverified','email':'user@example.com','provider':'password'})
    assert blocked_token.status_code == 503

def test_guest_and_firebase_free_config(client):
    guest = client.post('/api/auth/guest', json={'name':'Phone Guest'})
    assert guest.status_code == 200
    user = guest.json()
    assert user['is_guest'] == 1
    assert user['provider'] == 'anonymous'
    # Local demo mode accepts a well-formed JWT payload without needing a Firebase project id.
    header = lambda value: {'Authorization': f'Bearer {value}'}
    import base64, json as _json
    payload = base64.urlsafe_b64encode(_json.dumps({'sub':'firebase-demo-uid'}).encode()).decode().rstrip('=')
    token = f'aaa.{payload}.bbb'
    fb = client.post('/api/auth/firebase', json={'id_token': token, 'name':'Firebase Demo', 'email':'firebase@example.com', 'provider':'password'}, headers=header(token))
    assert fb.status_code == 200, fb.text
    assert fb.json()['provider'] == 'password'

def test_courses_start_incomplete_and_progress(client):
    user = login(client)
    courses = client.get('/api/courses', params={'user_id':user['id']}).json()
    assert len(courses) >= 9
    assert not courses[0]['lessons'][0]['completed']
    lesson_id = courses[0]['lessons'][0]['id']
    complete = client.post(f'/api/lessons/{lesson_id}/progress', params={'user_id':user['id']}, json={'completed':True})
    assert complete.status_code == 200
    lesson = client.get(f'/api/lessons/{lesson_id}', params={'user_id':user['id']}).json()
    assert lesson['completed'] is True
    assert complete.json()['xp'] >= 20

def test_bookmark_and_notes_are_lesson_specific(client):
    user = login(client, email='library@example.com', name='Library User')
    c = client.get('/api/courses', params={'user_id':user['id']}).json()[0]
    first, second = c['lessons'][0]['id'], c['lessons'][1]['id']
    b = client.post('/api/bookmarks', params={'user_id':user['id']}, json={'lesson_id':second})
    assert b.status_code == 200 and b.json()['bookmarked'] is True
    bookmarks = client.get(f'/api/bookmarks/{user["id"]}').json()
    assert [x['lesson_id'] for x in bookmarks] == [second]
    n = client.post('/api/notes', params={'user_id':user['id']}, json={'lesson_id':first,'content':'Remember this carefully.'})
    assert n.status_code == 200
    notes = client.get(f'/api/notes/{user["id"]}').json()
    assert notes[0]['lesson_id'] == first
    d = client.delete(f'/api/notes/{n.json()["id"]}', params={'user_id':user['id']})
    assert d.status_code == 200

def test_quiz_safe_payload_and_scoring(client):
    user = login(client, email='quiz@example.com', name='Quiz User')
    quizzes = client.get('/api/quizzes', params={'user_id':user['id']}).json()
    assert quizzes and quizzes[0]['question_count'] >= 3
    q = client.get(f'/api/quizzes/{quizzes[0]["id"]}', params={'user_id':user['id']}).json()
    assert q['questions'] and all('answer_index' not in item for item in q['questions'])
    answers = [0 for _ in q['questions']]
    r = client.post(f'/api/quizzes/{q["id"]}/attempt', params={'user_id':user['id']}, json={'answers':answers})
    assert r.status_code == 200
    assert r.json()['total'] == len(answers)

def test_flashcard_spaced_review_is_per_user_and_schedules_ratings(client):
    learner = login(client, email='reviewer@example.com', name='Review Learner')
    other = login(client, email='other-reviewer@example.com', name='Other Reviewer')
    queue_url = '/api/flashcards/review'
    queue = client.get(queue_url, params={'user_id':learner['id']})
    assert queue.status_code == 200
    initial = queue.json()
    assert initial['total_count'] > 0
    assert initial['due_count'] == initial['total_count']
    card_id = initial['cards'][0]['id']

    reviewed = client.post(f'/api/flashcards/{card_id}/review', params={'user_id':learner['id']}, json={'rating':'good'})
    assert reviewed.status_code == 200
    assert reviewed.json()['interval_days'] == 1
    after_first = client.get(queue_url, params={'user_id':learner['id']}).json()
    assert after_first['due_count'] == initial['due_count'] - 1
    assert next(card for card in after_first['cards'] if card['id'] == card_id)['due'] == 0
    assert client.get(queue_url, params={'user_id':other['id']}).json()['due_count'] == initial['due_count']

    for card, rating, interval in zip(initial['cards'][1:5], ['again','hard','good','easy'], [1/144,1,1,4]):
        result = client.post(f'/api/flashcards/{card["id"]}/review', params={'user_id':learner['id']}, json={'rating':rating})
        assert result.status_code == 200
        assert result.json()['interval_days'] == interval

    second_review = client.post(f'/api/flashcards/{card_id}/review', params={'user_id':learner['id']}, json={'rating':'good'})
    assert second_review.status_code == 200
    assert second_review.json()['interval_days'] == 3
    assert second_review.json()['repetitions'] == 2
    assert client.post(f'/api/flashcards/{card_id}/review', params={'user_id':learner['id']}, json={'rating':'unknown'}).status_code == 422
    assert client.post('/api/flashcards/missing-card/review', params={'user_id':learner['id']}, json={'rating':'good'}).status_code == 404

def test_teacher_classrooms_assignments_and_student_join(client):
    teacher = login(client, email='class-teacher@example.com', role='teacher', name='Class Teacher')
    student = login(client, email='class-student@example.com', name='Class Student')
    course = client.get('/api/courses', params={'user_id':student['id']}).json()[0]
    quiz = client.get('/api/quizzes', params={'user_id':student['id']}).json()[0]

    created = client.post('/api/classrooms', params={'user_id':teacher['id']}, json={'name':'Ganita Cohort'})
    assert created.status_code == 200
    classroom = created.json()
    assert len(classroom['join_code']) == 8
    assert client.post('/api/classrooms', params={'user_id':student['id']}, json={'name':'Unauthorized'}).status_code == 403

    assert client.get(f"/api/classrooms/{classroom['id']}/assignments", params={'user_id':student['id']}).status_code == 403
    joined = client.post('/api/classrooms/join', params={'user_id':student['id']}, json={'join_code':classroom['join_code'].lower()})
    assert joined.status_code == 200
    assert client.post('/api/classrooms/join', params={'user_id':student['id']}, json={'join_code':'NOTACODE'}).status_code == 404

    assigned_course = client.post(f"/api/classrooms/{classroom['id']}/assignments", params={'user_id':teacher['id']}, json={'course_id':course['id']})
    assigned_quiz = client.post(f"/api/classrooms/{classroom['id']}/assignments", params={'user_id':teacher['id']}, json={'quiz_id':quiz['id']})
    assert assigned_course.status_code == 200
    assert assigned_quiz.status_code == 200
    assert client.post(f"/api/classrooms/{classroom['id']}/assignments", params={'user_id':teacher['id']}, json={'course_id':course['id'],'quiz_id':quiz['id']}).status_code == 422
    assignments = client.get(f"/api/classrooms/{classroom['id']}/assignments", params={'user_id':student['id']})
    assert assignments.status_code == 200
    assert {x['course_id'] for x in assignments.json() if x['course_id']} == {course['id']}
    assert {x['quiz_id'] for x in assignments.json() if x['quiz_id']} == {quiz['id']}

def test_teacher_create_course_quiz_path(client):
    teacher = login(client, email='teacher-owner@example.com', role='teacher', name='Teacher Owner')
    lesson = {'title':'Test IKS lesson','sanskrit':'ज्ञानम्','iast':'jñānam','overview':'Know the term.','content':'A teacher-authored IKS lesson.','english_meaning':'Knowledge.','historical_context':'Historical study context.','modern_parallel':'Modern comparison kept separate.','source':'Teacher-authored source note','takeaways':['Understand the term.'],'word_by_word':['ज्ञान — jñāna — knowledge'],'practical':'Use it as a revision cue.','minutes':10,'difficulty':'Beginner','checkpoint':{'question':'What is this lesson about?','options':['Knowledge','Steel','Water','Astronomy'],'answer_index':0,'explanation':'ज्ञान means knowledge.'}}
    c = client.post('/api/courses', params={'user_id':teacher['id']}, json={'title':'IKS Teacher Course','category':'Philosophy','icon':'📘','level':'Beginner','duration':'1 week','description':'Teacher course','era':'Historical study','lessons':[lesson]})
    assert c.status_code == 200, c.text
    cid = c.json()['id']
    p = client.post('/api/paths', params={'user_id':teacher['id']}, json={'title':'IKS Teacher Path','weeks':2,'course_ids':[cid]})
    assert p.status_code == 200, p.text
    q = client.post('/api/quizzes', params={'user_id':teacher['id']}, json={'course_id':cid,'title':'IKS Teacher Quiz','description':'Checkpoint','questions':[{'question':'Pick IKS','options':['IKS','Other'],'answer_index':0,'explanation':'IKS is correct.'}]})
    assert q.status_code == 200, q.text
    learner = login(client, email='learner2@example.com', name='Learner 2')
    non_teacher = client.post('/api/courses', params={'user_id':learner['id']}, json={'title':'Should fail','lessons':[]})
    assert non_teacher.status_code == 403

def test_certificate_issued_after_course_completion(client):
    user = login(client, email='cert@example.com', name='Certificate User')
    course = client.get('/api/courses', params={'user_id':user['id']}).json()[0]
    for lesson in course['lessons']:
        r = client.post(f'/api/lessons/{lesson["id"]}/progress', params={'user_id':user['id']}, json={'completed':True})
        assert r.status_code == 200
    achievements = client.get(f'/api/achievements/{user["id"]}').json()
    assert achievements['certificates']
    cert_id = achievements['certificates'][0]['id']
    pdf = client.get(f'/api/certificates/{cert_id}/pdf', params={'user_id':user['id']})
    assert pdf.status_code == 200 and pdf.headers['content-type'].startswith('application/pdf')

def test_sanskrit_guide_and_ai_offline(client):
    user = login(client, email='ai@example.com', name='AI User')
    guide = client.get('/api/sanskrit-guide')
    assert guide.status_code == 200
    assert any(item[0]=='अ' for item in guide.json()['vowels'])
    r = client.post('/api/ai/ask', params={'user_id':user['id']}, json={'message':'Explain shunya in IKS'})
    assert r.status_code == 200
    body = r.json()
    assert 'zero' in body['answer'].lower() or 'śūnya' in body['answer'].lower()
    assert body['model'] is None or isinstance(body['model'], str)

def test_search_excludes_retired_content(client):
    user = login(client, email='search@example.com', name='Search User')
    r = client.get('/api/search', params={'q':'college syllabus','user_id':user['id']})
    assert r.status_code == 200
    body = r.json()
    assert body['courses'] == [] and body['lessons'] == [] and body['scholars'] == []

def test_firebase_guest_and_bearer_binding(client):
    import base64, json as _json
    payload = base64.urlsafe_b64encode(_json.dumps({'sub':'fb-guest-uid'}).encode()).decode().rstrip('=')
    token = f'a.{payload}.b'
    guest = client.post('/api/auth/firebase', json={'id_token':token,'name':'Firebase Guest','email':'','provider':'anonymous'}, headers={'Authorization':f'Bearer {token}'})
    assert guest.status_code == 200, guest.text
    assert guest.json()['is_guest'] == 1
    registered = guest.json()

    other = login(client, email='other-user@example.com', name='Other User')
    blocked = client.get(f'/api/users/{other["id"]}/dashboard', headers={'Authorization':f'Bearer {token}'})
    assert blocked.status_code == 403
    assert client.get(f'/api/users/{registered["id"]}/dashboard', headers={'Authorization':f'Bearer {token}'}).status_code == 200


def test_course_contains_source_metadata_not_retired_syllabus(client):
    user = login(client, email='sources@example.com', name='Sources User')
    courses = client.get('/api/courses', params={'user_id':user['id']}).json()
    assert courses
    assert any(c.get('texts') for c in courses)
    assert all('Data Visualization' not in (c.get('title','') + c.get('description','')) for c in courses)
