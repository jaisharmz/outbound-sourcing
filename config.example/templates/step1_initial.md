---
subject: "Research collaboration: {{ account.name }} x {{ persona.org }}"
---
<p>Hello {{ contact.first_name }}!</p>

<p><strong>Would it be possible to collaborate with your team at {{ account.name }} on a research project?</strong></p>

<p>My name is {{ persona.first_name }}, and I {{ persona.role }} at {{ persona.org }}. Here's a bit about what we've {% if document_links %}<a href="{{ document_links[0].url }}">worked on in the past</a>{% else %}worked on in the past{% endif %}:</p>

<p>
{% for project in persona.projects %}
<strong>- {{ project.org }}:</strong> {{ project.blurb }}<br>
{% endfor %}
</p>

{% if personalization %}
<p>{{ personalization }}</p>

{% endif %}
<p>I was wondering if it would be possible to collaborate on a research project with your group at {{ account.name }}. <strong>Are you free for a 15 minute chat?</strong></p>

<p>Sincerely,<br>
- {{ persona.name }}<br>
{% for label, url in persona.links.items() %}
<a href="{{ url }}">{{ label }}</a><br>
{% endfor %}
</p>
