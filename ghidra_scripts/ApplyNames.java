// Apply annotations/names.txt ("addr name" per line) to the program: rename the function at each
// address, creating it first (Thumb disassembly + createFunction) if auto-analysis didn't find it,
// e.g. code reached only through data tables. Idempotent. Prints a summary and any failures.
// args: <names.txt>
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.SourceType;
import java.math.BigInteger;
import java.nio.file.*;

public class ApplyNames extends GhidraScript {
	@Override
	public void run() throws Exception {
		Register tmode = currentProgram.getRegister("TMode");
		int renamed = 0, created = 0, same = 0, failed = 0;
		for (String line : Files.readAllLines(Paths.get(getScriptArgs()[0]))) {
			line = line.replaceAll("#.*", "").trim();
			if (line.isEmpty()) continue;
			String[] f = line.split("\\s+");
			Address a = toAddr(Long.parseLong(f[0], 16));
			Function fn = getFunctionAt(a);
			if (fn == null) {
				if (getInstructionAt(a) == null) {
					currentProgram.getProgramContext().setValue(tmode, a, a, BigInteger.ONE);
					disassemble(a);
				}
				fn = createFunction(a, null);
				if (fn == null) { println("FAILED to create function " + a + " for " + f[1]); failed++; continue; }
				created++;
			}
			if (fn.getName().equals(f[1])) { same++; continue; }
			try { fn.setName(f[1], SourceType.USER_DEFINED); renamed++; }
			catch (Exception e) { println("FAILED rename " + a + " -> " + f[1] + ": " + e.getMessage()); failed++; }
		}
		println(String.format("names: %d renamed, %d already set, %d functions created, %d failed",
			renamed, same, created, failed));
	}
}
