{% extends "base.html.j2" %}
{% block content %}
<div class="col-md-8 mx-auto">
  <h2 class="mb-3">👋 Introductions</h2>
  <p class="text-muted">Say who you are, so other members have something to talk about with you.
     Members who joined in the last {{ newcomer_days }} days are shown first - please give them a friendly welcome!</p>

  <div class="card shadow-sm mb-4">
    <div class="card-body">
      <h5 class="card-title">{% if has_intro %}Add another introduction{% else %}Introduce yourself{% endif %}</h5>
      <ul class="small text-muted">
        {% for p in prompts %}<li>{{ p }}</li>{% endfor %}
      </ul>
      <form action="{{ url_for('introductions_page') }}" method="POST">
        <textarea class="form-control mb-3" name="content" rows="4" placeholder="Hi, I'm {{ session.get('username') }}..." required></textarea>
        <button type="submit" class="btn btn-primary float-end">Post introduction</button>
      </form>
    </div>
  </div>

  {% for intro in intros %}
  <div class="card mb-3" id="intro-{{ intro.id }}">
    <div class="card-body">
      <div class="d-flex justify-content-between align-items-center">
        <h5 class="mb-0"><a href="/u/{{ intro.user_id }}">{{ intro.username }}</a>
          {% if intro.is_newcomer %}<span class="badge bg-warning text-dark">New member</span>{% endif %}
          {% if intro.location %}<small class="text-muted"> · {{ intro.location }}</small>{% endif %}
        </h5>
        <small class="text-muted">{{ intro.created_at|datetimeformat }}</small>
      </div>
      <p class="mt-2">{{ intro.content }}</p>

      {% for c in intro.comments %}
        <div class="border-start ps-2 mb-1"><b>{{ c.username }}:</b> {{ c.content }}</div>
      {% endfor %}

      <form action="{{ url_for('add_comment', post_id=intro.id) }}" method="POST" class="mt-2">
        <input type="hidden" name="next" value="{{ url_for('introductions_page') }}">
        <div class="input-group">
          <input type="text" class="form-control" name="content" placeholder="Say hello to {{ intro.username }}..." required>
          <button type="submit" class="btn btn-outline-primary">Reply</button>
        </div>
      </form>
    </div>
  </div>
  {% else %}
  <p class="text-muted">No introductions yet - be the first!</p>
  {% endfor %}
</div>
{% endblock %}
