from django import template

register = template.Library()


@register.filter
def to_hhmmss(value):
    """Decimal hours → HH:MM:SS string. Returns '' if value is falsy."""
    if not value:
        return ''
    try:
        total_seconds = round(float(value) * 3600)
        h = total_seconds // 3600
        m = (total_seconds % 3600) // 60
        s = total_seconds % 60
        return f'{h:02d}:{m:02d}:{s:02d}'
    except (ValueError, TypeError):
        return ''


@register.filter
def minutes_to_hhmmss(value):
    """Integer minutes → HH:MM:SS string. Returns '' if value is falsy."""
    if not value:
        return ''
    try:
        total_seconds = int(value) * 60
        h = total_seconds // 3600
        m = (total_seconds % 3600) // 60
        s = total_seconds % 60
        return f'{h:02d}:{m:02d}:{s:02d}'
    except (ValueError, TypeError):
        return ''
