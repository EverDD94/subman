package org.jellyfin.androidtv.ui.base

import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import org.jellyfin.design.Tokens

fun colorScheme(): ColorScheme = ColorScheme(
	background = Color(0xFF0B0D12),
	onBackground = Tokens.Color.colorBluegrey25,
	button = Color(0x24FFFFFF),
	onButton = Color(0xFFF4F2EE),
	buttonFocused = Color(0xFFF4F2EE),
	onButtonFocused = Color(0xFF000000),
	buttonDisabled = Color(0x12FFFFFF),
	onButtonDisabled = Color(0xFF6B707B),
	buttonActive = Color(0xFF3D63E0),
	onButtonActive = Color(0xFFFFFFFF),
	input = Color(0x1FFFFFFF),
	onInput = Color(0xFFF4F2EE),
	inputFocused = Color(0xFFF4F2EE),
	onInputFocused = Color(0xFF0B0D12),
	rangeControlBackground = Tokens.Color.colorBluegrey700,
	rangeControlFill = Color(0xFF3D63E0),
	rangeControlKnob = Tokens.Color.colorBluegrey100,
	seekbarBuffer = Tokens.Color.colorBluegrey300,
	recording = Tokens.Color.colorRed300,
	onRecording = Tokens.Color.colorRed25,
	badge = Color(0xFF3D63E0),
	onBadge = Tokens.Color.colorBluegrey100,
	listHeader = Tokens.Color.colorGrey50,
	listOverline = Tokens.Color.colorGrey500,
	listHeadline = Tokens.Color.colorGrey25,
	listCaption = Tokens.Color.colorGrey200,
	listButton = Color.Transparent,
	listButtonFocused = Color(0x24FFFFFF),
	surface = Color(0xFF161A22),
	scrim = Tokens.Color.colorBlack.copy(alpha = 0.67f),
)

@Immutable
data class ColorScheme(
	val background: Color,
	val onBackground: Color,

	val button: Color,
	val onButton: Color,
	val buttonFocused: Color,
	val onButtonFocused: Color,
	val buttonDisabled: Color,
	val onButtonDisabled: Color,
	val buttonActive: Color,
	val onButtonActive: Color,

	val input: Color,
	val onInput: Color,
	val inputFocused: Color,
	val onInputFocused: Color,

	val rangeControlBackground: Color,
	val rangeControlFill: Color,
	val rangeControlKnob: Color,
	val seekbarBuffer: Color,

	val recording: Color,
	val onRecording: Color,

	val badge: Color,
	val onBadge: Color,

	val listHeader: Color,
	val listOverline: Color,
	val listHeadline: Color,
	val listCaption: Color,
	val listButton: Color,
	val listButtonFocused: Color,

	val surface: Color,
	val scrim: Color,
)

val LocalColorScheme = staticCompositionLocalOf { colorScheme() }
