package org.jellyfin.androidtv.ui.search

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.jellyfin.androidtv.ui.base.Text

private val Ink = Color(0xFFF4F2EE)
private val Rows = listOf("abcdef", "ghijkl", "mnñopq", "rstuvw", "xyz123", "456789")

/**
 * Letters on screen, moved with the remote's arrows, so searching never pops up the TV keyboard.
 * Each press updates the search right away.
 */
@Composable
fun EverflixKeyboard(query: String, onQueryChange: (String) -> Unit) {
	Column(Modifier.width(276.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
		Text(
			text = query.ifEmpty { "Escribe para buscar" },
			color = if (query.isEmpty()) Ink.copy(alpha = 0.45f) else Ink,
			fontSize = 22.sp,
			fontWeight = FontWeight.W800,
			maxLines = 1,
			modifier = Modifier.padding(bottom = 10.dp),
		)
		Box(Modifier.fillMaxWidth().height(2.dp).background(Color(0xFF3D63E0)))
		Row(horizontalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.padding(top = 10.dp)) {
			Key("espacio", 136.dp) { onQueryChange("$query ") }
			Key("⌫ borrar", 136.dp) { onQueryChange(query.dropLast(1)) }
		}
		Rows.forEach { row ->
			Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
				row.forEach { c -> Key(c.toString(), 42.dp) { onQueryChange(query + c) } }
			}
		}
		Key("borrar todo", 276.dp) { onQueryChange("") }
	}
}

@Composable
private fun Key(label: String, width: Dp, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	Box(
		contentAlignment = Alignment.Center,
		modifier = Modifier
			.size(width, 40.dp)
			.clip(RoundedCornerShape(6.dp))
			.background(if (focused) Ink else Color.White.copy(alpha = 0.06f))
			.clickable(interactionSource = interaction, indication = null, onClick = onClick),
	) {
		Text(
			label.uppercase(),
			color = if (focused) Color.Black else Ink.copy(alpha = 0.85f),
			fontSize = if (label.length > 1) 12.sp else 17.sp,
			fontWeight = FontWeight.W700,
		)
	}
}
