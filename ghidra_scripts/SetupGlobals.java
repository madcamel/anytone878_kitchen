// Make RAM globals first-class in the decompiler:
//  1. flash block -> name FLASH, r-x (so the decompiler trusts literal-pool constants and folds
//     `*DAT_08011204` into a direct global access),
//  2. RAM block -> initialized from out/sram_init.bin (tools/raminit.py), writable,
//  3. every literal-pool word read by a pc-relative load whose value is in RAM/PERIPH/SCS
//     -> typed as a pointer (creates the reference, so globals get xrefs) and marked constant
//     (block r-x alone doesn't make the decompiler fold them),
//  4. names + types from annotations/globals.txt.
// args: <sram_init.bin> <globals.txt>
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.data.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.mem.*;
import ghidra.program.model.symbol.*;
import java.io.*;
import java.nio.file.*;
import java.util.*;

public class SetupGlobals extends GhidraScript {
	boolean inData(long v) {
		return (v >= 0x20000000L && v < 0x20018000L) || (v >= 0x40000000L && v < 0x40100000L)
			|| (v >= 0xE0000000L && v < 0xE0100000L);
	}

	DataType parseType(String t) {
		if (t.equals("-")) return null;
		int n = 0;
		if (t.contains("[")) {
			n = Integer.parseInt(t.substring(t.indexOf('[') + 1, t.indexOf(']')));
			t = t.substring(0, t.indexOf('['));
		}
		DataType base;
		switch (t) {
			case "u8": base = ByteDataType.dataType; break;
			case "u16": base = UnsignedShortDataType.dataType; break;
			case "u32": base = UnsignedIntegerDataType.dataType; break;
			case "ptr": base = PointerDataType.dataType; break;
			default: throw new IllegalArgumentException("type " + t);
		}
		return n > 0 ? new ArrayDataType(base, n, base.getLength()) : base;
	}

	@Override
	public void run() throws Exception {
		String[] a = getScriptArgs();
		Memory mem = currentProgram.getMemory();

		MemoryBlock flash = mem.getBlock(toAddr(0x08004000L));
		if (!flash.getName().equals("FLASH")) flash.setName("FLASH");
		flash.setRead(true); flash.setWrite(false); flash.setExecute(true);
		println("flash block: " + flash.getName() + " r-x");

		MemoryBlock ram = mem.getBlock(toAddr(0x20000000L));
		if (!ram.isInitialized()) {
			ram = mem.convertToInitialized(ram, (byte) 0);
			byte[] img = Files.readAllBytes(Paths.get(a[0]));
			mem.setBytes(toAddr(0x20000000L), Arrays.copyOf(img, (int) ram.getSize()));
			println("RAM initialized from " + a[0]);
		}
		ram.setRead(true); ram.setWrite(true); ram.setExecute(false);

		// 3. literal-pool pointers
		Listing L = currentProgram.getListing();
		int typed = 0, skipped = 0, constant = 0;
		Set<Address> pools = new HashSet<>();
		for (Instruction ins : L.getInstructions(true)) {
			if (monitor.isCancelled()) break;
			if (!ins.getMnemonicString().toLowerCase().startsWith("ldr")) continue;
			for (Reference r : ins.getReferencesFrom()) {
				Address p = r.getToAddress();
				if (r.getReferenceType().isRead() && flash.contains(p)) pools.add(p);
			}
		}
		for (Address p : pools) {
			if ((p.getOffset() & 3) != 0) continue;
			long v = mem.getInt(p) & 0xffffffffL;
			if (!inData(v)) continue;
			Data d = L.getDataAt(p);
			if (d == null || !d.isPointer()) {
				if (L.getInstructionContaining(p) != null) { skipped++; continue; }
				try {
					clearListing(p, p.add(3));
					d = createData(p, PointerDataType.dataType);
					typed++;
				} catch (Exception e) { skipped++; continue; }
			}
			// the decompiler folds a pool load into a direct global access only for constant data
			MutabilitySettingsDefinition.DEF.setChoice(d, MutabilitySettingsDefinition.CONSTANT);
			constant++;
		}
		println("literal-pool pointers typed: " + typed + ", marked constant: " + constant + " (skipped " + skipped + ")");

		// 4. names/types
		int named = 0;
		for (String line : Files.readAllLines(Paths.get(a[1]))) {
			line = line.replaceAll("#.*", "").trim();
			if (line.isEmpty()) continue;
			String[] f = line.split("\\s+");
			Address ad = toAddr(Long.parseLong(f[0], 16));
			SymbolTable st = currentProgram.getSymbolTable();
			for (Symbol s : st.getSymbols(ad))
				if (s.getSource() != SourceType.USER_DEFINED && !s.isPrimary()) s.delete();
			Symbol s = st.createLabel(ad, f[1], SourceType.USER_DEFINED);
			s.setPrimary();
			DataType dt = parseType(f.length > 2 ? f[2] : "-");
			if (dt != null) {
				clearListing(ad, ad.add(dt.getLength() - 1));
				createData(ad, dt);
			}
			named++;
		}
		println("globals named: " + named);
	}
}
