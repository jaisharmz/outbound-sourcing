---
subject: Would it be possible to collaborate with your team at {{ account.name }} on an AI research / engineering project?
---
Hello {{ contact.first_name }}!

I hope this email finds you well. My name is {{ persona.first_name }}, and I {{ persona.role }} at {{ persona.org }}. Here's a bit about what we've worked on in the past:

{{ persona.project_bullets }}
{% if personalization %}
{{ personalization }}
{% endif %}
I was wondering if it would be possible to collaborate on a research project with your group at {{ account.name }}. Are you free for a 15 minute chat to discuss a potential collaboration?
{% if document_links %}
A few things we've written, if useful:
{% for doc in document_links %}
- {{ doc.name }}: {{ doc.url }}
{%- endfor %}
{% endif %}
{{ persona.signature }}
